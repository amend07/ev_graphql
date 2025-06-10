def admin_required(func):
    def wrapper(self, info, *args, **kwargs):
        user = info.context.user
        if not user.is_authenticated or user.role != 'admin':
            raise Exception("Only admin can perform this action.")
        return func(self, info, *args, **kwargs)
    return wrapper
