def make_oauth(tag, expires_at=9_999_999_999_000):
    return {
        "accessToken": f"AT-{tag}",
        "refreshToken": f"RT-{tag}",
        "expiresAt": expires_at,
        "scopes": ["user:inference"],
        "subscriptionType": "pro",
    }
