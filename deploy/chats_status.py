"""Статус всех чатов из chats.txt под аккаунтом наблюдателя. Запускать ТОЛЬКО на сервере.

  python -m deploy.chats_status                # только проверка, ничего не меняет
  python -m deploy.chats_status --join 5       # вступить не более чем в 5 чатов, с паузами

Без --join скрипт только читает. С --join вступает в публичные чаты и по приглашениям, где аккаунта ещё нет;
при FloodWait сразу останавливается. Вступайте партиями по 3–5 чатов в день: массовые вступления — частая причина
блокировки аккаунта. Боты (@..._bot) скрипт не трогает: в каждом нужно вручную нажать Start."""
import argparse
import random
import time
from telethon.errors import FloodWaitError, UserAlreadyParticipantError
from telethon.tl.functions.channels import JoinChannelRequest
from telethon.tl.functions.messages import CheckChatInviteRequest, ImportChatInviteRequest
from telethon.tl.types import ChatInviteAlready, User
from leadbot.watcher import _client, load_chats, _INVITE

JOIN_PAUSE = (60, 120)  # секунд между вступлениями


def status(client, src):
    """member | not_member | bot | error:<текст>"""
    m = _INVITE.search(src)
    if m:
        inv = client(CheckChatInviteRequest(m.group(1)))
        return "member" if isinstance(inv, ChatInviteAlready) else "not_member"
    ent = client.get_entity(src.rsplit("/", 1)[-1].lstrip("@"))
    if isinstance(ent, User):
        return "bot" if ent.bot else "error:не чат"
    return "not_member" if getattr(ent, "left", False) else "member"


def join(client, src):
    m = _INVITE.search(src)
    try:
        if m:
            client(ImportChatInviteRequest(m.group(1)))
        else:
            client(JoinChannelRequest(client.get_entity(src.rsplit("/", 1)[-1].lstrip("@"))))
    except UserAlreadyParticipantError:
        pass


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--join", type=int, default=0, metavar="N", help="вступить максимум в N чатов")
    args = ap.parse_args()
    chats = load_chats()
    result = {}
    with _client() as client:
        if not client.is_user_authorized():
            raise SystemExit("Нет входа в аккаунт: python -m leadbot.watcher")
        for src in chats:
            try:
                result[src] = status(client, src)
            except FloodWaitError as exc:
                print(f"FloodWait {exc.seconds} с — останавливаюсь, повторите позже")
                break
            except Exception as exc:
                result[src] = "error:" + str(exc)[:80]
            time.sleep(1.5)
        joined = 0
        if args.join:
            for src, st in result.items():
                if st != "not_member" or joined >= args.join:
                    continue
                try:
                    join(client, src)
                    result[src] = "joined_now"
                    joined += 1
                    print(f"Вступил: {src}")
                except FloodWaitError as exc:
                    print(f"FloodWait {exc.seconds} с при вступлении — останавливаюсь")
                    break
                except Exception as exc:
                    result[src] = "error:" + str(exc)[:80]
                time.sleep(random.randint(*JOIN_PAUSE))
    names = {"member": "читается", "joined_now": "вступили сейчас", "not_member": "НЕ состоит",
             "bot": "бот: нажмите Start вручную"}
    for src in chats:
        st = result.get(src, "не проверен")
        print(f"{names.get(st, st):28} {src}")
    ok = sum(1 for s in result.values() if s in ("member", "joined_now"))
    print(f"\nЧитаются: {ok} из {len(chats)}")


if __name__ == "__main__":
    main()
