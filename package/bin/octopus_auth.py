"""How the add-on logs in to Octopus Kraken.

Kept free of Splunk imports so it can be unit tested on its own.
"""

API_KEY_PREFIX = "sk_"


def kraken_login_input(email, secret):
    """Input for the obtainKrakenToken mutation.

    The account's secret field takes either an Octopus API key (sk_live_...,
    from the developer page of the Octopus dashboard) or the account password.
    An API key logs in on its own; a password needs the account email, which
    is the account entry's name.
    """
    secret = (secret or "").strip()
    if secret.startswith(API_KEY_PREFIX):
        return {"APIKey": secret}
    return {"email": email, "password": secret}
