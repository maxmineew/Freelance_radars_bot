"""Обезличивание текста ДО отправки в LLM: email, телефоны, ссылки, @username, ФИО-подобные."""
import re

PHONE = re.compile(r"(?:\+?7|8)[\s(\-]*\d{3}[\s)\-]*\d{3}[\s\-]*\d{2}[\s\-]*\d{2}")

_RULES = [
    (re.compile(r"[\w.+-]+@[\w-]+\.[\w.-]+"), "[EMAIL]"),
    (PHONE, "[PHONE]"),
    (re.compile(r"https?://\S+|t\.me/\S+|www\.\S+"), "[LINK]"),
    (re.compile(r"(?<!\w)@\w{3,}"), "[USER]"),
    # «Иван Петров», «Анна Сергеевна»: два слова с заглавной подряд внутри предложения
    (re.compile(r"(?<=[a-zа-я,] )[А-ЯЁ][а-яё]{2,}\s+[А-ЯЁ][а-яё]{2,}\b"), "[NAME]"),
]


def anonymize(text: str) -> str:
    for rx, repl in _RULES:
        text = rx.sub(repl, text)
    return text
