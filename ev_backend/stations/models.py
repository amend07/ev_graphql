from django.db import models

# Create your models here.
class Station():
    name = models.CharField(max_length=20)
    