"""Encryption at rest for the credentials pilots connect to their account:
Navigraph sign-in tokens and Hoppie logon codes. A database leak alone
then doesn't give anyone access to those accounts.

Each kind of credential gets its own key, derived from the app's secret
key (FLASK_SECRET_KEY). If that key changes, stored credentials can't be
read any more: pilots simply sign in with Navigraph, or enter their Hoppie
code, again.
"""
import base64
import hashlib

from cryptography.fernet import Fernet, InvalidToken


class SecretBox:
    def __init__(self, secret, purpose):
        key = hashlib.sha256(f"virtualdispatch/{purpose}/".encode() + str(secret).encode()).digest()
        self._fernet = Fernet(base64.urlsafe_b64encode(key))

    def seal(self, text):
        return self._fernet.encrypt(text.encode()).decode()

    def open(self, sealed):
        """The original text, or None if it can't be decrypted."""
        try:
            return self._fernet.decrypt(sealed.encode()).decode()
        except (InvalidToken, AttributeError, ValueError):
            return None
