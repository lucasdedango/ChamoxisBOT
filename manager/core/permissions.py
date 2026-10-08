import os


def ids(name):
    return {int(value.strip()) for value in os.getenv(name, "").split(",") if value.strip()}


def allowed_user(user_id):
    allowlist = ids("DISCORD_ALLOWED_USER_IDS")
    return not allowlist or user_id in allowlist or user_id in ids("DISCORD_ADMIN_IDS")


def administrator(user_id):
    return user_id in ids("DISCORD_ADMIN_IDS")
