"""Demo data so the app is usable before Entra ID is wired up."""
from . import db

USERS = [
    ("ada.lovelace@example.com", "Ada Lovelace", "Head of Engineering", "Engineering"),
    ("grace.hopper@example.com", "Grace Hopper", "Principal Engineer", "Engineering"),
    ("alan.turing@example.com", "Alan Turing", "Security Engineer", "Security"),
    ("katherine.johnson@example.com", "Katherine Johnson", "Data Analyst", "Finance"),
    ("hedy.lamarr@example.com", "Hedy Lamarr", "Product Designer", "Design"),
    ("linus.pauling@example.com", "Linus Pauling", "IT Administrator", "IT"),
]

ASSETS = [
    ("MacBook Pro 14 M4", "Laptop", 249900, "C02X1234ADA", "ada.lovelace@example.com"),
    ("MacBook Air 15 M4", "Laptop", 159900, "C02X5678GRA", "grace.hopper@example.com"),
    ("ThinkPad X1 Carbon", "Laptop", 189000, "PF0ALAN01", "alan.turing@example.com"),
    ("Dell U2723QE 27\" 4K", "Monitor", 59900, "CN0MON001", "ada.lovelace@example.com"),
    ("Dell U2723QE 27\" 4K", "Monitor", 59900, "CN0MON002", "grace.hopper@example.com"),
    ("Dell U2723QE 27\" 4K", "Monitor", 59900, "CN0MON003", None),
    ("Logitech MX Master 3S", "Peripheral", 10900, None, "hedy.lamarr@example.com"),
    ("Keychron K3 Pro", "Peripheral", 9900, None, "alan.turing@example.com"),
    ("iPhone 16", "Phone", 96900, "IMEI-778812", "linus.pauling@example.com"),
    ("Jabra Evolve2 65 headset", "Peripheral", 22900, None, None),
    ("Adobe Photoshop perpetual", "Software", 27500, None, "hedy.lamarr@example.com"),
    ("Docking station CalDigit TS4", "Peripheral", 39900, None, "katherine.johnson@example.com"),
]

SUBS = [
    ("Microsoft 365 E3", "Microsoft", 3300, ["ada.lovelace@example.com", "grace.hopper@example.com", "alan.turing@example.com", "katherine.johnson@example.com", "hedy.lamarr@example.com", "linus.pauling@example.com"]),
    ("GitHub Enterprise", "GitHub", 1900, ["ada.lovelace@example.com", "grace.hopper@example.com", "alan.turing@example.com"]),
    ("Adobe Creative Cloud", "Adobe", 6499, ["hedy.lamarr@example.com"]),
    ("Slack Business+", "Salesforce", 1250, ["ada.lovelace@example.com", "grace.hopper@example.com", "hedy.lamarr@example.com", "linus.pauling@example.com"]),
    ("Atlassian Jira", "Atlassian", 790, ["ada.lovelace@example.com", "katherine.johnson@example.com"]),
    ("1Password Business", "AgileBits", 750, ["ada.lovelace@example.com", "grace.hopper@example.com", "alan.turing@example.com", "katherine.johnson@example.com", "hedy.lamarr@example.com", "linus.pauling@example.com"]),
]


def seed_if_empty() -> bool:
    """Populate demo rows only when the database has no users at all."""
    if db.q1("SELECT 1 FROM users LIMIT 1"):
        return False
    with db.cursor() as conn:
        for upn, name, title, dept in USERS:
            conn.execute(
                "INSERT INTO users (upn, display_name, job_title, department, source) VALUES (?,?,?,?,'seed')",
                (upn, name, title, dept),
            )
        for name, cat, cost, serial, upn in ASSETS:
            conn.execute(
                """INSERT INTO assets (name, category, cost_cents, serial, assigned_upn, assigned_on)
                   VALUES (?,?,?,?,?, CASE WHEN ? IS NULL THEN NULL ELSE date('now') END)""",
                (name, cat, cost, serial, upn, upn),
            )
        for name, vendor, cost, seats in SUBS:
            sid = conn.execute(
                "INSERT INTO subscriptions (name, vendor, monthly_cost_cents) VALUES (?,?,?)",
                (name, vendor, cost),
            ).lastrowid
            for upn in seats:
                conn.execute(
                    "INSERT INTO subscription_seats (subscription_id, upn, assigned_on) VALUES (?,?,date('now'))",
                    (sid, upn),
                )
    return True
