# app.py
from flask import Flask, render_template, request, redirect, url_for, session, flash, g
from werkzeug.security import generate_password_hash, check_password_hash
import sqlite3
from pathlib import Path
from datetime import datetime

BASE = Path(__file__).parent
DB_PATH = BASE / "blog.db"
SCHEMA_PATH = BASE / "schema.sql"

app = Flask(__name__)
app.secret_key = "CHANGE_THIS_SECRET_BEFORE_PROD"  # поменяй перед деплоем!

def get_db():
    db = getattr(g, "_db", None)
    if db is None:
        db = g._db = sqlite3.connect(str(DB_PATH))
        db.row_factory = sqlite3.Row
        # enable foreign keys
        db.execute("PRAGMA foreign_keys = ON;")
    return db

@app.teardown_appcontext
def close_db(exc):
    db = getattr(g, "_db", None)
    if db is not None:
        db.close()

def init_db():
    if not DB_PATH.exists():
        with sqlite3.connect(str(DB_PATH)) as conn:
            with open(str(SCHEMA_PATH), "r", encoding="utf-8") as f:
                conn.executescript(f.read())

@app.before_request
def load_user():
    g.user = None
    if "user_id" in session:
        row = get_db().execute("SELECT id, username FROM users WHERE id = ?", (session["user_id"],)).fetchone()
        if row:
            g.user = row

# -------------------------
# Auth: register / login / logout
# -------------------------
@app.route("/register", methods=["GET","POST"])
def register():
    if request.method == "POST":
        username = request.form.get("username","").strip()
        password = request.form.get("password","")
        if not username or not password:
            flash("Заполните все поля")
            return redirect(url_for("register"))
        db = get_db()
        exists = db.execute("SELECT 1 FROM users WHERE username = ?", (username,)).fetchone()
        if exists:
            flash("Пользователь уже существует")
            return redirect(url_for("register"))
        pw_hash = generate_password_hash(password)
        db.execute("INSERT INTO users (username, password) VALUES (?, ?)", (username, pw_hash))
        db.commit()
        flash("Регистрация прошла успешно. Войдите.")
        return redirect(url_for("login"))
    return render_template("register.html")

@app.route("/login", methods=["GET","POST"])
def login():
    if request.method == "POST":
        username = request.form.get("username","").strip()
        password = request.form.get("password","")
        db = get_db()
        user = db.execute("SELECT * FROM users WHERE username = ?", (username,)).fetchone()
        if not user or not check_password_hash(user["password"], password):
            flash("Неверный логин или пароль")
            return redirect(url_for("login"))
        session["user_id"] = user["id"]
        flash("Вы вошли")
        return redirect(url_for("index"))
    return render_template("login.html")

@app.route("/logout")
def logout():
    session.clear()
    flash("Вы вышли")
    return redirect(url_for("index"))

# -------------------------
# Posts: create, view, edit, delete
# -------------------------
@app.route("/")
def index():
    """
    Показывает публичные посты. Query params:
      - feed=1  -> лента подписок (если авторизован)
      - tag=<name> -> фильтр по тегу
      - sort=recent|popular
    """
    feed = request.args.get("feed")
    tag = request.args.get("tag")
    sort = request.args.get("sort","recent")
    db = get_db()

    select_sql = """
      SELECT p.*, u.username,
        (SELECT COUNT(*) FROM comments c WHERE c.post_id = p.id) as comments_count
      FROM posts p JOIN users u ON u.id = p.user_id
    """
    where = []
    params = []
    if feed == "1" and g.user:
        where.append("(p.user_id = ? OR p.user_id IN (SELECT followed_id FROM subscriptions WHERE follower_id = ?))")
        params.extend([g.user["id"], g.user["id"]])
where.append("(p.public IN (1,0))")  # feed can include posts with privacy=0; view layer will enforce "by-request"
    else:
        where.append("p.public = 1")
    if tag:
        where.append("EXISTS (SELECT 1 FROM post_tags pt JOIN tags t ON t.id = pt.tag_id WHERE pt.post_id = p.id AND t.name = ?)")
        params.append(tag)
    where_sql = " WHERE " + " AND ".join(where) if where else ""
    if sort == "popular":
        order_sql = " ORDER BY comments_count DESC, p.created_at DESC"
    else:
        order_sql = " ORDER BY p.created_at DESC"
    rows = db.execute(select_sql + where_sql + order_sql, params).fetchall()
    posts = [dict(r) for r in rows]
    # attach tags
    for p in posts:
        p["tags"] = [t["name"] for t in db.execute("SELECT t.name FROM post_tags pt JOIN tags t ON t.id = pt.tag_id WHERE pt.post_id = ?", (p["id"],)).fetchall()]
    return render_template("index.html", posts=posts, feed=feed, tag=tag, sort=sort)

@app.route("/post/new", methods=["GET","POST"])
def new_post():
    if not g.user:
        return redirect(url_for("login"))
    if request.method == "POST":
        title = request.form.get("title","").strip()
        content = request.form.get("content","").strip()
        public = 1 if request.form.get("public") == "1" else 0  # checkbox/radio
        tags_raw = request.form.get("tags","")
        db = get_db()
        cur = db.execute("INSERT INTO posts (user_id, title, content, public, created_at) VALUES (?, ?, ?, ?, ?)",
                         (g.user["id"], title, content, public, datetime.utcnow().isoformat()))
        post_id = cur.lastrowid
        # tags handling: ensure tag exists in tags table, then link in post_tags
        tags = [t.strip().lower() for t in tags_raw.split(",") if t.strip()]
        for name in tags:
            row = db.execute("SELECT id FROM tags WHERE name = ?", (name,)).fetchone()
            if row:
                tag_id = row["id"]
            else:
                tag_id = db.execute("INSERT INTO tags (name) VALUES (?)", (name,)).lastrowid
            db.execute("INSERT INTO post_tags (post_id, tag_id) VALUES (?, ?)", (post_id, tag_id))
        db.commit()
        flash("Пост создан")
        return redirect(url_for("view_post", post_id=post_id))
    return render_template("new_post.html")

@app.route("/post/<int:post_id>")
def view_post(post_id):
    db = get_db()
    post = db.execute("SELECT p.*, u.username FROM posts p JOIN users u ON u.id = p.user_id WHERE p.id = ?", (post_id,)).fetchone()
    if not post:
        return "Пост не найден", 404
    # access rules: public(1) -> everyone; public(0) -> only author and users who have an approved request
    can_view = False
    if post["public"] == 1:
        can_view = True
    else:
        if g.user:
            if g.user["id"] == post["user_id"]:
                can_view = True
            else:
                req = db.execute("SELECT 1 FROM requests WHERE post_id = ? AND user_id = ?", (post_id, g.user["id"])).fetchone()
                if req:
                    can_view = True
    tags = [t["name"] for t in db.execute("SELECT t.name FROM post_tags pt JOIN tags t ON t.id = pt.tag_id WHERE pt.post_id = ?", (post_id,)).fetchall()]
    comments = db.execute("SELECT c.*, u.username FROM comments c JOIN users u ON u.id = c.user_id WHERE c.post_id = ? ORDER BY c.created_at", (post_id,)).fetchall()
    is_following = False
    if g.user:
        is_following = db.execute("SELECT 1 FROM subscriptions WHERE follower_id = ? AND followed_id = ?", (g.user["id"], post["user_id"])).fetchone() is not None
    return render_template("view_post.html", post=post, tags=tags, comments=comments, can_view=can_view, is_following=is_following)

@app.route("/post/<int:post_id>/request_access", methods=["POST"])
def request_access(post_id):
    if not g.user:
        return redirect(url_for("login"))
    db = get_db()
    exists = db.execute("SELECT 1 FROM requests WHERE post_id = ? AND user_id = ?", (post_id, g.user["id"])).fetchone()
    if not exists:
        db.execute("INSERT INTO requests (post_id, user_id, created_at) VALUES (?, ?, ?)", (post_id, g.user["id"], datetime.utcnow().isoformat()))
        db.commit()
        flash("Запрос отправлен автору")
    else:
        flash("Вы уже отправляли запрос")
    return redirect(url_for("view_post", post_id=post_id))

@app.route("/post/<int:post_id>/comment", methods=["POST"])
def add_comment(post_id):
    if not g.user:
        return redirect(url_for("login"))
    text = request.form.get("text","").strip()
    if not text:
        flash("Комментарий пуст")
        return redirect(url_for("view_post", post_id=post_id))
    db = get_db()
    db.execute("INSERT INTO comments (post_id, user_id, content, created_at) VALUES (?, ?, ?, ?)",
               (post_id, g.user["id"], text, datetime.utcnow().isoformat()))
    db.commit()
    flash("Комментарий добавлен")
    return redirect(url_for("view_post", post_id=post_id))

@app.route("/post/<int:post_id>/edit", methods=["GET","POST"])
def edit_post(post_id):
    if not g.user:
        return redirect(url_for("login"))
    db = get_db()
    post = db.execute("SELECT * FROM posts WHERE id = ?", (post_id,)).fetchone()
    if not post or post["user_id"] != g.user["id"]:
        return "Доступ запрещён", 403
    if request.method == "POST":
        title = request.form.get("title","").strip()
        content = request.form.get("content","").strip()
        public = 1 if request.form.get("public") == "1" else 0
        tags_raw = request.form.get("tags","")
        db.execute("UPDATE posts SET title = ?, content = ?, public = ? WHERE id = ?", (title, content, public, post_id))
        db.execute("DELETE FROM post_tags WHERE post_id = ?", (post_id,))
        tags = [t.strip().lower() for t in tags_raw.split(",") if t.strip()]
        for name in tags:
            row = db.execute("SELECT id FROM tags WHERE name = ?", (name,)).fetchone()
            if row:
                tag_id = row["id"]
            else:
                tag_id = db.execute("INSERT INTO tags (name) VALUES (?)", (name,)).lastrowid
            db.execute("INSERT INTO post_tags (post_id, tag_id) VALUES (?, ?)", (post_id, tag_id))
        db.commit()
        flash("Пост обновлён")
        return redirect(url_for("view_post", post_id=post_id))
    # GET: prepare tag string
    tags = [t["name"] for t in db.execute("SELECT t.name FROM post_tags pt JOIN tags t ON t.id = pt.tag_id WHERE pt.post_id = ?", (post_id,)).fetchall()]
    tagstr = ", ".join(tags)
    return render_template("edit_post.html", post=post, tags=tagstr)

@app.route("/post/<int:post_id>/delete", methods=["POST"])
def delete_post(post_id):
    if not g.user:
        return redirect(url_for("login"))
    db = get_db()
    post = db.execute("SELECT * FROM posts WHERE id = ?", (post_id,)).fetchone()
    if not post or post["user_id"] != g.user["id"]:
        return "Доступ запрещён", 403
    db.execute("DELETE FROM posts WHERE id = ?", (post_id,))
    db.commit()
    flash("Пост удалён")
    return redirect(url_for("index"))

# -------------------------
# Subscriptions (follow/unfollow) and feed
# -------------------------
@app.route("/user/<int:user_id>")
def profile(user_id):
    db = get_db()
    user = db.execute("SELECT id, username FROM users WHERE id = ?", (user_id,)).fetchone()
    if not user:
        return "Пользователь не найден", 404
    posts = db.execute("SELECT * FROM posts WHERE user_id = ? ORDER BY created_at DESC", (user_id,)).fetchall()
    followers = db.execute("SELECT u.id, u.username FROM subscriptions s JOIN users u ON u.id = s.follower_id WHERE s.followed_id = ?", (user_id,)).fetchall()
following = db.execute("SELECT u.id, u.username FROM subscriptions s JOIN users u ON u.id = s.followed_id WHERE s.follower_id = ?", (user_id,)).fetchall()
    is_following = False
    if g.user:
        is_following = db.execute("SELECT 1 FROM subscriptions WHERE follower_id = ? AND followed_id = ?", (g.user["id"], user_id)).fetchone() is not None
    return render_template("profile.html", user=user, posts=posts, followers=followers, following=following, is_following=is_following)

@app.route("/follow/<int:user_id>", methods=["POST"])
def follow(user_id):
    if not g.user:
        return redirect(url_for("login"))
    if user_id == g.user["id"]:
        flash("Нельзя подписаться на себя")
        return redirect(url_for("profile", user_id=user_id))
    db = get_db()
    exists = db.execute("SELECT 1 FROM subscriptions WHERE follower_id = ? AND followed_id = ?", (g.user["id"], user_id)).fetchone()
    if not exists:
        db.execute("INSERT INTO subscriptions (follower_id, followed_id) VALUES (?, ?)", (g.user["id"], user_id))
        db.commit()
        flash("Вы подписались")
    return redirect(url_for("profile", user_id=user_id))

@app.route("/unfollow/<int:user_id>", methods=["POST"])
def unfollow(user_id):
    if not g.user:
        return redirect(url_for("login"))
    db = get_db()
    db.execute("DELETE FROM subscriptions WHERE follower_id = ? AND followed_id = ?", (g.user["id"], user_id))
    db.commit()
    flash("Вы отписались")
    return redirect(url_for("profile", user_id=user_id))

# -------------------------
# Tags list and filter redirect
# -------------------------
@app.route("/tags")
def tags():
    db = get_db()
    rows = db.execute("SELECT name, COUNT(*) as cnt FROM tags t JOIN post_tags pt ON t.id = pt.tag_id GROUP BY name ORDER BY cnt DESC").fetchall()
    return render_template("tags.html", tags=rows)

@app.route("/tag/<name>")
def tag_view(name):
    return redirect(url_for("index", tag=name))

# -------------------------
# Requests management (author sees requests to their private posts)
# -------------------------
@app.route("/my_requests")
def my_requests():
    if not g.user:
        return redirect(url_for("login"))
    db = get_db()
    rows = db.execute("""
        SELECT r.*, u.username as requester_name, p.title
        FROM requests r
        JOIN users u ON u.id = r.user_id
        JOIN posts p ON p.id = r.post_id
        WHERE p.user_id = ?
        ORDER BY r.created_at DESC
    """, (g.user["id"],)).fetchall()
    return render_template("requests.html", rows=rows)

@app.route("/requests/<int:request_id>/grant", methods=["POST"])
def grant_request(request_id):
    if not g.user:
        return redirect(url_for("login"))
    db = get_db()
    req = db.execute("SELECT r.*, p.user_id FROM requests r JOIN posts p ON p.id = r.post_id WHERE r.id = ?", (request_id,)).fetchone()
    if not req or req["user_id"] != g.user["id"]:
        return "Доступ запрещён", 403
    # granting modeled as deleting request (we assume presence of request equals permission)
    db.execute("DELETE FROM requests WHERE id = ?", (request_id,))
    db.commit()
    flash("Доступ разрешён (запрос удалён).")
    return redirect(url_for("my_requests"))

# -------------------------
# Run
# -------------------------
if __name__ == "__main__":
    init_db()
    app.run(debug=True)