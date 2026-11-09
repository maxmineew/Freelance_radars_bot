"""Synthetic live checks; never sends cards or stores these examples as leads."""
from dotenv import load_dotenv
load_dotenv("/opt/leadbot/.env")
from leadbot import classifier, db

c = db.connect()
cases = (
    ("clinic_need", "У нас стоматологическая клиника. Администраторы не успевают перезванивать пациентам и подтверждать запись. Ищем подрядчика для автоматического обзвона нашей клиентской базы.", True),
    ("vendor_advertisement", "Мы внедряем голосовых роботов в стоматологии! Наш робот обзванивает пациентов, заменяет операторов и увеличивает прибыль. Закажите внедрение у нас, скидка 20 процентов.", False),
)
for label, text, expected in cases:
    card = classifier.classify(text, chat="Синтетическая проверка", state=c)
    print(label, "is_lead=" + str(card["is_lead"]), "score=" + str(card["score"]), "service=" + card["service"], "model=" + str(db.get_meta(c,"llm:last_model")))
    assert card["is_lead"] == expected, label
    if expected:
        assert card["score"] >= 6 and card["service"] == "voice_agent", label
c.close()
print("LIVE_LLM_CHECK_OK")
