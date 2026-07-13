def admin_required(func):
    def wrapper(self, info, *args, **kwargs):
        user = info.context.user
        if not user.is_authenticated or user.role != 'admin':
            raise Exception("Only admin can perform this action.")
        return func(self, info, *args, **kwargs)
    return wrapper


def active_required(func):
    """Require an authenticated AND active account.

    ``graphql_jwt``'s ``login_required`` only checks ``is_authenticated``; a
    deactivated user still holds a valid JWT until it expires and would
    otherwise keep acting. This closes that gap for every mutating operation.
    """
    def wrapper(self, info, *args, **kwargs):
        user = info.context.user
        if not user.is_authenticated:
            raise Exception("Authentication required.")
        if not user.is_active:
            raise Exception("This account is deactivated.")
        return func(self, info, *args, **kwargs)
    return wrapper


def station_owner_required(func):
    def wrapper(self, info, *args, **kwargs):
        user = info.context.user
        if not user.is_authenticated or user.role != 'station_owner' or not user.is_active:
            raise Exception("Only approved station owners can perform this action.")
        return func(self, info, *args, **kwargs)
    return wrapper
