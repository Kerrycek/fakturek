# MariaDB na Debianu (bez Dockeru)

Aplikace používá SQLAlchemy + PyMySQL, takže se připojuje přes standardní MySQL/MariaDB protokol.

## 1) Instalace a spuštění služby

```bash
sudo apt update
sudo apt install -y mariadb-server mariadb-client
sudo systemctl enable --now mariadb
sudo systemctl status mariadb --no-pager
```

Rychlá kontrola, že DB poslouchá:

```bash
ss -lntp | grep 3306 || true
```

## 2) Vytvoření databáze a účtu

Na Debianu bývá `root` přihlášení přes unix socket, takže typicky stačí:

```bash
sudo mariadb
```

A pak:

```sql
CREATE DATABASE fakturek CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci;

-- pro lokální dev doporučuju vytvořit účet pro oba hosty
-- (MySQL/MariaDB rozlišuje 'user'@'localhost' vs 'user'@'127.0.0.1')
CREATE USER 'fakturek'@'localhost' IDENTIFIED BY 'fakturek';
CREATE USER 'fakturek'@'127.0.0.1' IDENTIFIED BY 'fakturek';

GRANT ALL PRIVILEGES ON fakturek.* TO 'fakturek'@'localhost';
GRANT ALL PRIVILEGES ON fakturek.* TO 'fakturek'@'127.0.0.1';
FLUSH PRIVILEGES;
```

## 3) Nastavení aplikace

```bash
cp .env.example .env
```

Typicky stačí:

```bash
DATABASE_URL=mysql+pymysql://fakturek:fakturek@127.0.0.1:3306/fakturek?charset=utf8mb4
```

Pokud chceš místo TCP použít unix socket (Debian default), jde to takhle:

```bash
DATABASE_URL=mysql+pymysql://fakturek:fakturek@localhost/fakturek?unix_socket=/run/mysqld/mysqld.sock&charset=utf8mb4
```

## 4) Migrace

```bash
alembic -c alembic.ini upgrade head
```

## 5) Spuštění

```bash
uvicorn fakturek.main:app --reload --port 8000
```

## Troubleshooting

- `Access denied for user 'fakturek'@'127.0.0.1'`:
  - vytvoř i uživatele `'fakturek'@'127.0.0.1'` (viz výše), nebo změň DSN na unix socket.
- `Can't connect to MySQL server on '127.0.0.1'`:
  - zkontroluj, že běží služba `mariadb` a že DB poslouchá na 3306 (`ss -lntp | grep 3306`).
