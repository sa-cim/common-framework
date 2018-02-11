# Generated by Django 2.0.2 on 2018-02-10 19:53

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('common', '0006_auto_20170905'),
    ]

    operations = [
        migrations.AlterField(
            model_name='global',
            name='object_id',
            field=models.TextField(editable=False, verbose_name='identifiant'),
        ),
        migrations.AlterField(
            model_name='history',
            name='object_id',
            field=models.TextField(editable=False, verbose_name='identifiant'),
        ),
        migrations.AlterField(
            model_name='metadata',
            name='object_id',
            field=models.TextField(verbose_name='identifiant'),
        ),
    ]
