"""Request text classifier for deletion intent detection.

Determines whether a request_text authorizes data deletion or is a non-deletion
action (access, correction, export, marketing opt-out, inquiry, etc.).
"""

from __future__ import annotations

import re
from typing import Tuple


# Patterns that indicate NON-deletion intent (access, correction, export, etc.)
_NON_DELETION_PATTERNS = [
    # Access / download / export requests
    re.compile(
        r"\b(want|need|request|like)\s+(to\s+)?(access|download|export|obtain|receive|get)\s+(a\s+copy|copy|my\s+data|all\s+data|personal\s+data|information)",
        re.IGNORECASE,
    ),
    re.compile(r"\b(send|provide|share)\s+(me\s+)?(a\s+copy|my\s+data|all\s+data|the\s+data)", re.IGNORECASE),
    re.compile(r"\b(data\s+portability|data\s+export|subject\s+access)\b", re.IGNORECASE),
    re.compile(r"\bcopy\s+of\s+(my|all)\s+(personal\s+)?(data|information|records)", re.IGNORECASE),
    # Correction / rectification requests
    re.compile(r"\b(correct|rectif|update|change|fix|amend)\s+(my|the|personal)\s+(data|info|name|address|email|phone|details)", re.IGNORECASE),
    re.compile(r"\b(data\s+correction|data\s+rectification)\b", re.IGNORECASE),
    # Marketing opt-out / unsubscribe
    re.compile(r"\b(opt[\s-]?out|unsubscribe|stop\s+sending|stop\s+emails|remove\s+from\s+mailing)", re.IGNORECASE),
    re.compile(r"\b(marketing\s+opt[\s-]?out|email\s+preferences|no\s+more\s+emails|no\s+more\s+marketing)", re.IGNORECASE),
    # Generic inquiry / question (not asking for deletion)
    re.compile(r"\b(what\s+data|which\s+data|how\s+do\s+you\s+use)\s+(do\s+you\s+have|is\s+stored|are\s+you\s+keeping)", re.IGNORECASE),
    re.compile(r"\b(want\s+to\s+know|like\s+to\s+know|please\s+tell\s+me)\s+(what|which|how)", re.IGNORECASE),
    # Restrict processing
    re.compile(r"\brestrict\s+(the\s+)?processing\b", re.IGNORECASE),
]

# Patterns that CONFIRM deletion intent
_DELETION_PATTERNS = [
    # English
    re.compile(r"\b(delete|delet|remove|erase|wipe|purge|destroy)\s+(my|the|all|this|any|every)?\s*(account|data|profile|information|records|personal)", re.IGNORECASE),
    re.compile(r"\b(account\s+clos|close\s+my\s+account|account\s+delet|cancel\s+my\s+account)", re.IGNORECASE),
    re.compile(r"\bremov(e|al)\s+(of\s+)?(personal|my|the)\s+(data|info|details|records)", re.IGNORECASE),
    re.compile(r"\b(right\s+to\s+erasure|right\s+to\s+be\s+forgotten|data\s+erasure|data\s+deletion)\b", re.IGNORECASE),
    re.compile(r"\b(permanently\s+delete|full\s+deletion|complete\s+deletion|complete\s+the\s+deletion)\b", re.IGNORECASE),
    re.compile(r"\b(gdpr|ccpa|lgpd|pdpa|popia|dpa|privacy)\s+(deletion|erasure|removal)\b", re.IGNORECASE),
    # Close / terminate account
    re.compile(r"\b(close|shut\s+down|terminate|deactivate)\s+(my\s+|this\s+|the\s+)?account\b", re.IGNORECASE),
    # "haven't used" + "close" / "remove"
    re.compile(r"\bhaven'?t\s+used\b.*\b(close|remove|delete)", re.IGNORECASE),
    # Spanish
    re.compile(r"\b(eliminar|borrar|suprimir)\s+(mi|la|el|los|las|esta|todo)?\s*(cuenta|datos|perfil|información)", re.IGNORECASE),
    re.compile(r"\b(solicito|pido|deseo|quiero)\s+(la\s+)?(eliminación|supresión|borrado)", re.IGNORECASE),
    re.compile(r"\bcomplet(ar|en|e)\s+(la\s+)?eliminaci[oó]n\b", re.IGNORECASE),
    # French
    re.compile(r"\b(supprimer|effacer|suppression)\s+(mon|ma|mes|le|la|les|du|de)?\s*(compte|données|profil|informations)", re.IGNORECASE),
    re.compile(r"\b(demande|je\s+demande)\s+(la\s+)?suppression\b", re.IGNORECASE),
    # Portuguese
    re.compile(r"\b(excluir|apagar|remover|deletar)\s+(minha|meu|os|as|a|o)?\s*(conta|dados|perfil|informaç)", re.IGNORECASE),
    re.compile(r"\b(solicito|peço)\s+(a\s+)?(exclusão|remoção|apagamento)", re.IGNORECASE),
    # German
    re.compile(r"\b(löschen|entfernen|löschung)\s+(mein|meiner|meine|meines|das|die|den)?\s*(konto|daten|profil)", re.IGNORECASE),
    re.compile(r"\b(recht\s+auf\s+löschung|konto\s+löschen|datenlöschung)\b", re.IGNORECASE),
    # Japanese (simple)
    re.compile(r"(削除|消去|退会|アカウント.*削除)", re.IGNORECASE),
    # Korean
    re.compile(r"(삭제|계정.*삭제|탈퇴)", re.IGNORECASE),
    # Arabic (simplified)
    re.compile(r"(حذف|إزالة|مسح)", re.IGNORECASE),
    # Confirmation-based wording
    re.compile(r"\breceived\s+a\s+confirmation\b.*\bremov", re.IGNORECASE),
    re.compile(r"\bremaining\s+(copy|copies|data|records|profile)", re.IGNORECASE),
]


class RequestClassifier:
    """Deterministic classifier for deletion vs non-deletion request intent.

    Returns (is_deletion: bool, reason: str) for a given request_text.
    """

    def classify(self, request_text: str) -> Tuple[bool, str]:
        """Classify whether request_text authorizes deletion.

        Parameters
        ----------
        request_text:
            The raw request text from deletion_requests.csv.

        Returns
        -------
        (is_deletion, reason) — True if the request authorizes deletion.
        """
        if not request_text or not request_text.strip():
            return False, "Empty request text; cannot determine deletion intent."

        text = request_text.strip()

        # First check for explicit non-deletion intent
        for pattern in _NON_DELETION_PATTERNS:
            m = pattern.search(text)
            if m:
                # But check if there's ALSO a deletion pattern — if both match,
                # deletion intent wins (e.g. "delete my data and send me a copy")
                has_deletion = any(dp.search(text) for dp in _DELETION_PATTERNS)
                if not has_deletion:
                    return False, f"Non-deletion intent detected: '{m.group(0).strip()[:60]}'"

        # Check for deletion intent
        for pattern in _DELETION_PATTERNS:
            m = pattern.search(text)
            if m:
                return True, f"Deletion intent confirmed: '{m.group(0).strip()[:60]}'"

        # Default: if we can't determine intent, treat as non-deletion (conservative)
        # But in practice, most requests in the dataset are deletion requests
        # Check for any deletion-adjacent keywords as a weaker signal
        deletion_keywords = [
            "delete", "delet", "remov", "eras", "purg", "wipe",
            "close", "cancel", "terminat", "suppress", "elimin",
            "borr", "lösch", "exclu", "apag", "削除", "삭제", "حذف",
        ]
        text_lc = text.lower()
        for kw in deletion_keywords:
            if kw in text_lc:
                return True, f"Deletion keyword '{kw}' found in request text."

        return False, "No deletion intent detected in request text."
