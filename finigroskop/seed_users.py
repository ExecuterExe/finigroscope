"""Заведение пользователей в базу (регистрации на сайте нет).

Список участников и администраторов формируется заранее — например, из заявок
конкурса. Здесь это делается вручную для разработки и демонстрации.

Запуск:  python seed_users.py
"""

from app import app
from models import User, db

# (telegram-ник, персональный пароль, роль, отображаемое имя)
SEED = [
    ("@designer",  "demo-pass-1",  User.ROLE_PARTICIPANT, "Демо-геймдизайнер"),
    ("@user1",     "x7k2-mq9",     User.ROLE_PARTICIPANT, "Участник 1"),
    ("@admin",     "admin-2026",   User.ROLE_ADMIN,       "Администратор ИФТП"),
]


def seed():
    with app.app_context():
        db.create_all()
        created, skipped = 0, 0
        for tag, password, role, name in SEED:
            tag = User.normalize_tag(tag)
            if User.query.filter_by(tg_tag=tag).first():
                skipped += 1
                continue
            user = User(tg_tag=tag, role=role, display_name=name)
            user.set_password(password)
            db.session.add(user)
            created += 1
        db.session.commit()
        print(f"Готово: добавлено {created}, пропущено (уже были) {skipped}.")
        print("Учётные данные для входа:")
        for tag, password, role, _ in SEED:
            print(f"  {tag:12s} / {password:14s} [{role}]")


if __name__ == "__main__":
    seed()
