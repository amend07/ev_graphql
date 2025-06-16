# create_admin_user.py

import os
import django
import getpass

# Setup Django environment
os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'ev_backend.settings')
django.setup()

from django.contrib.auth import get_user_model

User = get_user_model()

def create_admin():
    print("🔐 Create a new admin user\n")

    username = input("Enter username: ")
    email = input("Enter email: ")
    
    # Secure password input
    while True:
        password = getpass.getpass("Enter password: ")
        confirm = getpass.getpass("Confirm password: ")
        if password == confirm:
            break
        print("❌ Passwords do not match. Try again.\n")

    if User.objects.filter(username=username).exists():
        print("⚠️ User with that username already exists.")
        return

    admin_user = User.objects.create_user(
        username=username,
        email=email,
        password=password,
    )

    # Set permissions and role
    admin_user.is_staff = True
    admin_user.is_superuser = True
    admin_user.role = 'admin'
    admin_user.save()

    print(f"✅ Admin user '{username}' created successfully with role='admin'.")

if __name__ == "__main__":
    create_admin()
