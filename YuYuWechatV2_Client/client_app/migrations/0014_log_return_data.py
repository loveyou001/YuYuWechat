# Generated by Django 4.1 on 2024-08-01 02:16

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('client_app', '0013_log'),
    ]

    operations = [
        migrations.AddField(
            model_name='log',
            name='return_data',
            field=models.TextField(default='null'),
        ),
    ]