from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [("core", "0004_initial_roles")]

    operations = [
        migrations.AddConstraint(
            model_name="user",
            constraint=models.CheckConstraint(
                condition=models.Q(("is_superuser", False)), name="core_user_no_superuser"
            ),
        ),
    ]
