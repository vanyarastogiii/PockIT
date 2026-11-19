from django.db import migrations


def create_holds_wallet(apps, schema_editor):
    Wallet = apps.get_model("wallets", "Wallet")
    Wallet.objects.get_or_create(kind="SYSTEM", name="risk_holds")


class Migration(migrations.Migration):
    dependencies = [("wallets", "0003_risk_holds_and_system_wallets")]
    operations = [migrations.RunPython(create_holds_wallet, migrations.RunPython.noop)]
