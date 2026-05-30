from __future__ import annotations

import os


class PilotManager:
    def __init__(self, raw_users: str | None = None) -> None:
        self.raw_users = os.getenv("PILOT_USERS", "") if raw_users is None else raw_users
        self._users = self._parse_users(self.raw_users)

    def is_pilot_user(self, user: str) -> bool:
        if not self._users:
            return True
        return user in self._users

    def allowed_domains(self, user: str) -> list[str]:
        domains = self._users.get(user)
        if domains is None:
            return []
        return domains or ["*"]

    def is_domain_allowed(self, user: str, domain: str | None) -> bool:
        domains = self.allowed_domains(user)
        if not domains or "*" in domains or domain is None:
            return bool(domains)
        return domain in domains

    def _parse_users(self, raw_users: str) -> dict[str, list[str]]:
        users: dict[str, list[str]] = {}
        for entry in [part.strip() for part in raw_users.split(",") if part.strip()]:
            if ":" in entry:
                user, domain_text = entry.split(":", 1)
                users[user.strip()] = [domain.strip() for domain in domain_text.split("|") if domain.strip()]
            else:
                users[entry] = ["*"]
        return users

