# user_auth/serializers.py
from rest_framework import serializers
from .models import User, EmailVerificationCode, Organization, OrganizationMember, ClientAccount, ClientMember


class EmailRequestSerializer(serializers.Serializer):
    email = serializers.EmailField()


class CodeVerificationSerializer(serializers.Serializer):
    email = serializers.EmailField()
    code = serializers.CharField(max_length=6)


class OrganizationSerializer(serializers.ModelSerializer):
    class Meta:
        model = Organization
        fields = ["name", "email", "website"]


class OrganizationMemberSerializer(serializers.ModelSerializer):
    email = serializers.EmailField(source="user.email", read_only=True)
    new_email = serializers.EmailField(write_only=True, required=False)

    class Meta:
        model = OrganizationMember
        fields = ["id", "full_name", "email", "new_email", "role"]

    def create(self, validated_data):
        new_email = validated_data.pop("new_email", None)
        if not new_email:
            raise serializers.ValidationError({"new_email": "Email is required to add a member."})
        user, _ = User.objects.get_or_create(email=new_email)
        org = self.context["organization"]
        return OrganizationMember.objects.create(
            organization=org,
            user=user,
            full_name=validated_data["full_name"],
            role=validated_data["role"],
        )

    def update(self, instance, validated_data):
        new_email = self.initial_data.get("new_email")
        if new_email and new_email != instance.user.email:
            if User.objects.filter(email=new_email).exists():
                raise serializers.ValidationError({"new_email": "This email is already in use."})
            instance.user.email = new_email
            instance.user.save()
        instance.full_name = validated_data.get("full_name", instance.full_name)
        instance.role = validated_data.get("role", instance.role)
        instance.save()
        return instance


# ── Client Serializers ────────────────────────────────────────────────────────

class ClientAccountSerializer(serializers.ModelSerializer):
    waba_connected = serializers.SerializerMethodField()
    member_count   = serializers.SerializerMethodField()

    class Meta:
        model = ClientAccount
        fields = [
            "id", "name", "email", "website", "industry",
            "waba_id", "phone_number_id", "waba_name", "phone_number",
            "status", "waba_connected", "member_count",
            "created_at", "updated_at",
        ]
        read_only_fields = [
            "id", "waba_id", "phone_number_id", "waba_name", "phone_number",
            "waba_connected", "member_count", "created_at", "updated_at",
        ]

    def get_waba_connected(self, obj):
        return obj.waba_connected()

    def get_member_count(self, obj):
        return obj.members.count()


class ClientAccountCreateSerializer(serializers.ModelSerializer):
    """Used when TechNova creates a new client."""
    class Meta:
        model = ClientAccount
        fields = ["name", "email", "website", "industry"]


class ClientMemberSerializer(serializers.ModelSerializer):
    email     = serializers.EmailField(source="user.email", read_only=True)
    new_email = serializers.EmailField(write_only=True, required=False)

    class Meta:
        model = ClientMember
        fields = ["id", "full_name", "email", "new_email", "role", "created_at"]
        read_only_fields = ["id", "email", "created_at"]

    def create(self, validated_data):
        new_email = validated_data.pop("new_email", None)
        if not new_email:
            raise serializers.ValidationError({"new_email": "Email is required."})

        # Prevent a user from being in multiple places
        user, _ = User.objects.get_or_create(email=new_email)
        if hasattr(user, "organization") or hasattr(user, "membership") or hasattr(user, "client_membership"):
            raise serializers.ValidationError(
                {"new_email": "This user is already part of another organization or client."}
            )

        client = self.context["client"]
        return ClientMember.objects.create(
            client=client,
            user=user,
            full_name=validated_data["full_name"],
            role=validated_data["role"],
        )

    def update(self, instance, validated_data):
        instance.full_name = validated_data.get("full_name", instance.full_name)
        instance.role = validated_data.get("role", instance.role)
        instance.save()
        return instance