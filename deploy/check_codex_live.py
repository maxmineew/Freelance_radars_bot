"""Synthetic classification smoke test; does not read queues or send messages."""
import os
os.environ["LLM_BACKEND"] = "codex_cli"
from leadbot import classifier

assert classifier.provider_list()[0][0] == "codex_cli"
card = classifier.classify(
    "Я владелец стоматологии. Ищу разработчика голосового бота для обзвона "
    "пациентов и подтверждения записи. Нужно подключить к нашей CRM. "
    "Бюджет 100000 рублей, начать хотим на этой неделе.")
assert card["is_lead"] and card["score"] >= 6, card
assert card["service"] == "voice_agent", card
print("PASS: provider=codex_cli, valid lead card, score=" + str(card["score"]))
