from django.db import migrations

class Migration(migrations.Migration):
    dependencies = [
        ("discount", "0053_drop_sales_indexes"),
    ]
    operations = [
        migrations.RunSQL("DROP TABLE IF EXISTS discount_sale CASCADE;", migrations.RunSQL.noop),
        migrations.RunSQL("DROP TABLE IF EXISTS discount_sale_categories CASCADE;", migrations.RunSQL.noop),
        migrations.RunSQL("DROP TABLE IF EXISTS discount_sale_collections CASCADE;", migrations.RunSQL.noop),
        migrations.RunSQL("DROP TABLE IF EXISTS discount_sale_products CASCADE;", migrations.RunSQL.noop),
        migrations.RunSQL("DROP TABLE IF EXISTS discount_sale_variants CASCADE;", migrations.RunSQL.noop),
        migrations.RunSQL("DROP TABLE IF EXISTS discount_saletranslation CASCADE;", migrations.RunSQL.noop),
        migrations.RunSQL("DROP TABLE IF EXISTS discount_salechannellisting CASCADE;", migrations.RunSQL.noop),
    ]
