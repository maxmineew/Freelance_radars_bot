"""Приоритетные заявки: с телефоном в тексте и заказы из Яндекс Услуг.

Сам номер нигде не сохраняется — только флаг «phone» (152-ФЗ).
Работает и с исходным текстом, и с обезличенным (там телефон уже заменён на [PHONE]).
"""
import re

from .anonymize import PHONE

YANDEX_EXCHANGE = "Яндекс Услуги"
YANDEX = re.compile(r"uslugi\.yandex|яндекс[\s.\-]*(?:услуг|исполнител)|я\.?\s?услуг", re.I)


def flags(text, exchange=None):
    """Возвращает 'phone', 'yandex', 'phone,yandex' или None."""
    text = text or ""
    found = []
    if "[PHONE]" in text or PHONE.search(text):
        found.append("phone")
    if exchange == YANDEX_EXCHANGE or YANDEX.search(text):
        found.append("yandex")
    return ",".join(found) or None
