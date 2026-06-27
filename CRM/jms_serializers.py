from rest_framework import serializers
from .models import ChatSession, ChatMessage


class ChatMessageSerializer(serializers.ModelSerializer):
    class Meta:
        model  = ChatMessage
        fields = ["id", "role", "content", "source", "created_at"]


class ChatSessionSerializer(serializers.ModelSerializer):
    messages = ChatMessageSerializer(many=True, read_only=True)

    class Meta:
        model  = ChatSession
        fields = ["id", "title", "created_at", "messages"]


class AskSerializer(serializers.Serializer):
    question   = serializers.CharField(allow_blank=True, required=False, default="")
    session_id = serializers.IntegerField(required=False, allow_null=True)