from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):
    dependencies = [("CRM", "0022_avantikatemplate_address_font_size_and_more")]

    operations = [
        migrations.CreateModel(
            name="CallSession",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("call_id", models.CharField(max_length=255, unique=True)),
                ("phone_number_id", models.CharField(db_index=True, max_length=100)),
                ("caller", models.CharField(blank=True, max_length=255)),
                ("recipient", models.CharField(blank=True, max_length=255)),
                ("status", models.CharField(choices=[("incoming", "Incoming"), ("pre_accepted", "Pre-accepted"), ("accepted", "Accepted"), ("rejected", "Rejected"), ("terminated", "Terminated"), ("failed", "Failed")], default="incoming", max_length=20)),
                ("sdp_offer", models.TextField(blank=True)),
                ("sdp_answer", models.TextField(blank=True)),
                ("terminate_status", models.CharField(blank=True, max_length=30)),
                ("metadata", models.JSONField(blank=True, default=dict)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                ("connected_at", models.DateTimeField(blank=True, null=True)),
                ("terminated_at", models.DateTimeField(blank=True, null=True)),
                ("client", models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name="calls", to="CRM.clientaccount")),
            ],
        ),
    ]