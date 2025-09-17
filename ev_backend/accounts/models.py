from django.db import models
from django.contrib.auth.models import AbstractUser


class User(AbstractUser):
    ROLE_CHOICES = (
        ('admin', 'Admin'),
        ('station_owner', 'Station Owner'),
        ('user', 'User'),
    )
    role = models.CharField(max_length=20, choices=ROLE_CHOICES, default='user')
    
    def is_station_owner(self):
        return self.role == 'station_owner'
    
    def __str__(self):
        return f"{self.username} ({self.role})"
