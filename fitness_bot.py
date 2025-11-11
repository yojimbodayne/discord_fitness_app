import os
import math
import asyncio
import datetime as dt

import discord
from discord import app_commands
from discord.ext import commands
from dotenv import load_dotenv
import aiosqlite

import random
from discord.ext import commands, tasks  # you already have commands, just make sure tasks is there

# -----------------------
# CONFIG
# -----------------------

load_dotenv()
TOKEN = os.getenv("DISCORD_TOKEN")

INTENTS = discord.Intents.default()
INTENTS.members = True
INTENTS.message_content = True  # needed for wait_for("message")

def today_str():
    return dt.datetime.utcnow().strftime("%Y-%m-%d")

def yesterday_str():
    y = dt.datetime.utcnow().date() - dt.timedelta(days=1)
    return y.strftime("%Y-%m-%d")

POINTS_VERSION = "v1.3-weekly-streaks"
# Remember who we've already reminded today (guild_id, user_id) -> date_str
DAILY_REMINDER_CACHE: dict[tuple[int, int], str] = {}

# -----------------------
# BOT SETUP
# -----------------------

bot = commands.Bot(command_prefix="!", intents=INTENTS)
tree = bot.tree

DB_PATH = "fitness_points.db"


async def init_db():
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            """
            CREATE TABLE IF NOT EXISTS logs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                username TEXT NOT NULL,
                date TEXT NOT NULL,
                category TEXT NOT NULL,
                value REAL NOT NULL,
                points REAL NOT NULL,
                created_at TEXT NOT NULL
            )
            """
        )
        await db.commit()


# -----------------------
# POINTS LOGIC
# -----------------------

def calc_strength_points(minutes: int) -> float:
    if minutes < 30:
        return 0.0
    if minutes < 45:
        return 1.0
    if minutes < 60:
        return 1.25
    return 1.5


def calc_cardio_points(minutes: int, steps: int | None = None) -> float:
    """
    Running / cardio / steps -> points.
    10,000 steps = meets 30-min run criteria (1 pt).
    15,000+ steps -> 1.5 pts.
    """
    if steps is not None:
        if steps < 10_000:
            return 0.0
        if steps < 15_000:
            return 1.0
        return 1.5

    if minutes < 30:
        return 0.0
    if minutes < 45:
        return 1.0
    if minutes < 60:
        return 1.25
    return 1.5


def calc_sleep_points(hours: float) -> float:
    if hours < 6:
        return 0.0
    if hours < 8:
        return 1.0
    return 2.0


def calc_protein_points(heavy_meals: int, protein_shakes: int) -> float:
    pts = heavy_meals * 1.0 + protein_shakes * 0.5
    return min(pts, 1.5)


def calc_supplement_points(vitamins: bool, creatine: bool, magnesium: bool, omega3: bool) -> float:
    count = sum([vitamins, creatine, magnesium, omega3])
    return count * 0.25  # up to 1.0


def calc_water_points(ounces: int) -> float:
    return 0.5 if ounces >= 80 else 0.0


def calc_alcohol_penalty(drinks: int) -> float:
    if drinks < 3:
        return 0.0
    return -1.0 * (drinks // 3)


def calc_pastry_penalty(pastries: int) -> float:
    return -1.0 * pastries


def calc_fastfood_penalty(meals: int) -> float:
    return -1.0 * meals


# -----------------------
# DB HELPERS
# -----------------------

async def add_log(user: discord.User | discord.Member, date: str, category: str, value: float, points: float):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            """
            INSERT INTO logs (user_id, username, date, category, value, points, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                user.id,
                str(user),
                date,
                category,
                float(value),
                float(points),
                dt.datetime.utcnow().isoformat(),
            ),
        )
        await db.commit()


async def daily_points_for_user(user_id: int, date: str) -> float:
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            "SELECT SUM(points) FROM logs WHERE user_id = ? AND date = ?",
            (user_id, date),
        ) as cursor:
            row = await cursor.fetchone()
            return row[0] if row and row[0] is not None else 0.0


async def daily_breakdown_for_user(user_id: int, date: str):
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            """
            SELECT category, SUM(points) 
            FROM logs 
            WHERE user_id = ? AND date = ?
            GROUP BY category
            ORDER BY category
            """,
            (user_id, date),
        ) as cursor:
            rows = await cursor.fetchall()
            return rows


async def leaderboard_for_range(start_date: str, end_date: str, limit: int = 10):
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            """
            SELECT user_id, username, SUM(points) AS total_pts
            FROM logs
            WHERE date BETWEEN ? AND ?
            GROUP BY user_id, username
            ORDER BY total_pts DESC
            LIMIT ?
            """,
            (start_date, end_date, limit),
        ) as cursor:
            rows = await cursor.fetchall()
            return rows


async def weekly_totals_for_user(user_id: int, start_date: str, end_date: str):
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            """
            SELECT date, SUM(points) AS total_pts
            FROM logs
            WHERE user_id = ? AND date BETWEEN ? AND ?
            GROUP BY date
            ORDER BY date
            """,
            (user_id, start_date, end_date),
        ) as cursor:
            rows = await cursor.fetchall()
            return rows


# -----------------------
# STREAK HELPERS
# -----------------------

def streak_badge(streak: int) -> str:
    """
    Returns a text badge for a given streak length.
    """
    if streak >= 30:
        return "🚀 30-Day Legend"
    if streak >= 21:
        return "👑 21-Day Monarch"
    if streak >= 14:
        return "🐉 14-Day Beast"
    if streak >= 7:
        return "🏅 7-Day Warrior"
    if streak >= 5:
        return "💪 5-Day Grinder"
    if streak >= 3:
        return "🔥 3-Day Spark"
    return "✨ No badge yet — keep going!"


async def current_and_best_streak_for_user(
    user_id: int,
    threshold: float = 4.0,
    max_days: int = 90,
):
    """
    Compute current streak and best streak over the last max_days, where a 'good day'
    is any day with total points >= threshold.
    """
    end = dt.datetime.utcnow().date()
    start = end - dt.timedelta(days=max_days - 1)
    start_str = start.strftime("%Y-%m-%d")
    end_str = end.strftime("%Y-%m-%d")

    rows = await weekly_totals_for_user(user_id, start_str, end_str)
    # rows: list of (date_str, total_pts)
    totals_map = {date_str: pts for (date_str, pts) in rows}

    # current streak: count backward from today until a break
    current_streak = 0
    for offset in range(max_days):
        d = end - dt.timedelta(days=offset)
        ds = d.strftime("%Y-%m-%d")
        pts = totals_map.get(ds, 0.0)
        if pts >= threshold:
            current_streak += 1
        else:
            break

    # best streak over whole window
    best_streak = 0
    running = 0
    for offset in range(max_days):
        d = start + dt.timedelta(days=offset)
        ds = d.strftime("%Y-%m-%d")
        pts = totals_map.get(ds, 0.0)
        if pts >= threshold:
            running += 1
            if running > best_streak:
                best_streak = running
        else:
            running = 0

    return current_streak, best_streak


# -----------------------
# ON READY
# -----------------------

@bot.event
async def on_ready():
    await init_db()

    # Sync commands per guild so new ones (like /quote) show up immediately
    for guild in bot.guilds:
        try:
            await tree.sync(guild=guild)
            cmd_names = [cmd.name for cmd in tree.get_commands()]
            print(f"Synced commands to guild {guild.name} ({guild.id}): {cmd_names}")
        except Exception as e:
            print(f"Error syncing commands for guild {guild.name} ({guild.id}): {e}")

    print(f"Logged in as {bot.user} (ID: {bot.user.id})")
    print(f"Points logic version: {POINTS_VERSION}")

    if not daily_drops_task.is_running():
        daily_drops_task.start()
        print("Started daily_drops_task.")




# -----------------------
# HELPER: ASK / WAIT FOR INPUT
# -----------------------

async def ask_number(
    interaction: discord.Interaction,
    prompt: str,
    allow_float: bool = False,
    default: float | int | None = 0,
    min_val: float | int | None = 0,
    max_val: float | int | None = None,
):
    channel = interaction.channel
    user = interaction.user

    await channel.send(
        f"{user.mention} {prompt} "
        f"(type a number, or `skip` to use `{default}`)"
    )

    def check(msg: discord.Message):
        return msg.author == user and msg.channel == channel

    while True:
        try:
            msg = await bot.wait_for("message", timeout=120.0, check=check)
        except asyncio.TimeoutError:
            await channel.send(f"{user.mention} timed out, using default `{default}`.")
            return default

        content = msg.content.strip().lower()
        if content == "skip":
            return default

        try:
            val = float(content) if allow_float else int(content)
        except ValueError:
            await channel.send("Please enter a valid number or `skip`.")
            continue

        if min_val is not None and val < min_val:
            await channel.send(f"Please enter a value ≥ {min_val}, or `skip`.")
            continue
        if max_val is not None and val > max_val:
            await channel.send(f"Please enter a value ≤ {max_val}, or `skip`.")
            continue

        return val


async def ask_yesno(interaction: discord.Interaction, prompt: str, default: bool = False):
    channel = interaction.channel
    user = interaction.user
    default_str = "yes" if default else "no"

    await channel.send(
        f"{user.mention} {prompt} (yes/no, or `skip` to use `{default_str}`)"
    )

    def check(msg: discord.Message):
        return msg.author == user and msg.channel == channel

    while True:
        try:
            msg = await bot.wait_for("message", timeout=120.0, check=check)
        except asyncio.TimeoutError:
            await channel.send(f"{user.mention} timed out, using default `{default_str}`.")
            return default

        content = msg.content.strip().lower()
        if content == "skip":
            return default
        if content in ("y", "yes"):
            return True
        if content in ("n", "no"):
            return False

        await channel.send("Please reply with `yes`, `no`, or `skip`.")


# -----------------------
# HELPER: GUIDED CHECK-IN
# -----------------------

async def run_checkin_dialog(interaction: discord.Interaction, date: str, label: str):
    """
    Guided Q&A for all metrics for a given date.
    label: 'today' or 'yesterday' etc. (just used in messages)
    """
    user = interaction.user
    channel = interaction.channel

    # Lifting
    lift_min = await ask_number(
        interaction,
        f"For **{label}**, how many **minutes of lifting/strength** did you do?",
        allow_float=False,
        default=0,
        min_val=0,
        max_val=300,
    )
    if lift_min > 0:
        pts = calc_strength_points(int(lift_min))
        await add_log(user, date, "strength", lift_min, pts)

    # Cardio minutes
    cardio_min = await ask_number(
        interaction,
        f"For **{label}**, how many **minutes of running/cardio** did you do? "
        "(If you only want steps, use `0` here).",
        allow_float=False,
        default=0,
        min_val=0,
        max_val=300,
    )

    # Steps
    steps = await ask_number(
        interaction,
        f"For **{label}**, how many **steps** did you take?",
        allow_float=False,
        default=0,
        min_val=0,
        max_val=100_000,
    )

    # cardio/steps logging
    if steps > 0:
        pts_steps = calc_cardio_points(minutes=0, steps=int(steps))
        await add_log(user, date, "steps", steps, pts_steps)
    if cardio_min > 0:
        pts_cardio = calc_cardio_points(minutes=int(cardio_min), steps=None)
        await add_log(user, date, "cardio", cardio_min, pts_cardio)

    # Sleep
    sleep_hours = await ask_number(
        interaction,
        f"For **{label}**, how many **hours of sleep** did you get (e.g. 7.5)?",
        allow_float=True,
        default=0.0,
        min_val=0.0,
        max_val=16.0,
    )
    if sleep_hours > 0:
        pts_sleep = calc_sleep_points(float(sleep_hours))
        await add_log(user, date, "sleep", sleep_hours, pts_sleep)

    # Protein
    heavy_meals = await ask_number(
        interaction,
        f"For **{label}**, how many **heavy protein meals** "
        "(chicken/steak/fish/4+ eggs) did you have?",
        allow_float=False,
        default=0,
        min_val=0,
        max_val=10,
    )
    shakes = await ask_number(
        interaction,
        f"For **{label}**, how many **protein shakes** did you drink?",
        allow_float=False,
        default=0,
        min_val=0,
        max_val=10,
    )
    if heavy_meals > 0 or shakes > 0:
        pts_protein = calc_protein_points(int(heavy_meals), int(shakes))
        await add_log(user, date, "protein", heavy_meals + shakes, pts_protein)

    # Supplements
    vitamins = await ask_yesno(interaction, f"For **{label}**, did you take your **vitamin**?", default=False)
    creatine = await ask_yesno(interaction, f"For **{label}**, did you take **creatine**?", default=False)
    magnesium = await ask_yesno(interaction, f"For **{label}**, did you take **magnesium**?", default=False)
    omega3 = await ask_yesno(interaction, f"For **{label}**, did you take **omega-3**?", default=False)
    if any([vitamins, creatine, magnesium, omega3]):
        pts_supp = calc_supplement_points(vitamins, creatine, magnesium, omega3)
        count = sum([vitamins, creatine, magnesium, omega3])
        await add_log(user, date, "supplements", count, pts_supp)

    # Water
    water_oz = await ask_number(
        interaction,
        f"For **{label}**, how many **ounces of water** did you drink?",
        allow_float=False,
        default=0,
        min_val=0,
        max_val=300,
    )
    if water_oz > 0:
        pts_water = calc_water_points(int(water_oz))
        await add_log(user, date, "water", water_oz, pts_water)

    # Alcohol
    drinks = await ask_number(
        interaction,
        f"For **{label}**, how many **alcoholic drinks** did you have?",
        allow_float=False,
        default=0,
        min_val=0,
        max_val=30,
    )
    if drinks > 0:
        pts_alc = calc_alcohol_penalty(int(drinks))
        await add_log(user, date, "alcohol", drinks, pts_alc)

    # Pastries
    pastries = await ask_number(
        interaction,
        f"For **{label}**, how many **pastries/desserts** did you eat?",
        allow_float=False,
        default=0,
        min_val=0,
        max_val=20,
    )
    if pastries > 0:
        pts_pastry = calc_pastry_penalty(int(pastries))
        await add_log(user, date, "pastry", pastries, pts_pastry)

    # Fast food
    fast_meals = await ask_number(
        interaction,
        f"For **{label}**, how many **fast-food meals** did you eat?",
        allow_float=False,
        default=0,
        min_val=0,
        max_val=10,
    )
    if fast_meals > 0:
        pts_ff = calc_fastfood_penalty(int(fast_meals))
        await add_log(user, date, "fastfood", fast_meals, pts_ff)

    # Final summary
    total = await daily_points_for_user(user.id, date)
    breakdown = await daily_breakdown_for_user(user.id, date)

    lines = [f"✅ **Check-in complete** for {user.mention} on **{date}**:"]
    if breakdown:
        for category, pts in breakdown:
            lines.append(f"- **{category}**: {pts:.2f} pts")
    lines.append(f"\n**Total for {label}:** `{total:.2f} pts`")

    await channel.send("\n".join(lines))


# -----------------------
# SLASH COMMANDS: LOGGING
# -----------------------

@tree.command(name="log_lift", description="Log a strength training / lifting session.")
@app_commands.describe(
    minutes="How many minutes did you lift weights?"
)
async def log_lift(interaction: discord.Interaction, minutes: app_commands.Range[int, 1, 300]):
    pts = calc_strength_points(minutes)
    await add_log(interaction.user, today_str(), "strength", minutes, pts)
    total = await daily_points_for_user(interaction.user.id, today_str())

    await interaction.response.send_message(
        f"💪 Logged **{minutes} min** of lifting for **{pts:.2f} pts**.\n"
        f"Your total for today is now **{total:.2f} pts**."
    )


@tree.command(name="log_run", description="Log running / cardio by minutes.")
@app_commands.describe(
    minutes="How many minutes did you run or do cardio?"
)
async def log_run(interaction: discord.Interaction, minutes: app_commands.Range[int, 1, 300]):
    pts = calc_cardio_points(minutes, steps=None)
    await add_log(interaction.user, today_str(), "cardio", minutes, pts)
    total = await daily_points_for_user(interaction.user.id, today_str())

    await interaction.response.send_message(
        f"🏃 Logged **{minutes} min** of cardio for **{pts:.2f} pts**.\n"
        f"Your total for today is now **{total:.2f} pts**."
    )


@tree.command(name="log_steps", description="Log your steps for today.")
@app_commands.describe(
    steps="Total steps today."
)
async def log_steps(interaction: discord.Interaction, steps: app_commands.Range[int, 1, 100_000]):
    pts = calc_cardio_points(minutes=0, steps=steps)
    await add_log(interaction.user, today_str(), "steps", steps, pts)
    total = await daily_points_for_user(interaction.user.id, today_str())

    await interaction.response.send_message(
        f"👣 Logged **{steps} steps** for **{pts:.2f} pts**.\n"
        f"Your total for today is now **{total:.2f} pts**."
    )


@tree.command(name="log_sleep", description="Log your sleep duration for last night (in hours).")
@app_commands.describe(
    hours="Hours of sleep (e.g. 7.5)."
)
async def log_sleep(interaction: discord.Interaction, hours: app_commands.Range[float, 0.0, 16.0]):
    pts = calc_sleep_points(hours)
    await add_log(interaction.user, today_str(), "sleep", hours, pts)
    total = await daily_points_for_user(interaction.user.id, today_str())

    await interaction.response.send_message(
        f"😴 Logged **{hours:.1f} hours** of sleep for **{pts:.2f} pts**.\n"
        f"Your total for today is now **{total:.2f} pts**."
    )


@tree.command(name="log_protein", description="Log your protein intake.")
@app_commands.describe(
    heavy_meals="Number of heavy protein meals (chicken/steak/fish/4+ eggs).",
    shakes="Number of protein shakes."
)
async def log_protein(
    interaction: discord.Interaction,
    heavy_meals: app_commands.Range[int, 0, 10],
    shakes: app_commands.Range[int, 0, 10]
):
    pts = calc_protein_points(heavy_meals, shakes)
    await add_log(interaction.user, today_str(), "protein", heavy_meals + shakes, pts)
    total = await daily_points_for_user(interaction.user.id, today_str())

    await interaction.response.send_message(
        f"🍗 Logged **{heavy_meals} heavy meals** and **{shakes} shakes** "
        f"for **{pts:.2f} pts** (capped at 1.5).\n"
        f"Your total for today is now **{total:.2f} pts**."
    )


@tree.command(name="log_supplements", description="Log supplements for today.")
@app_commands.describe(
    vitamins="Did you take your multivitamin?",
    creatine="Did you take creatine?",
    magnesium="Did you take magnesium?",
    omega3="Did you take omega-3?"
)
async def log_supplements(
    interaction: discord.Interaction,
    vitamins: bool,
    creatine: bool,
    magnesium: bool,
    omega3: bool
):
    pts = calc_supplement_points(vitamins, creatine, magnesium, omega3)
    count = sum([vitamins, creatine, magnesium, omega3])
    await add_log(interaction.user, today_str(), "supplements", count, pts)
    total = await daily_points_for_user(interaction.user.id, today_str())

    await interaction.response.send_message(
        f"💊 Logged **{count}** supplements for **{pts:.2f} pts**.\n"
        f"Your total for today is now **{total:.2f} pts**."
    )


@tree.command(name="log_water", description="Log your water intake (oz).")
@app_commands.describe(
    ounces="Total ounces of water today."
)
async def log_water(interaction: discord.Interaction, ounces: app_commands.Range[int, 0, 300]):
    pts = calc_water_points(ounces)
    await add_log(interaction.user, today_str(), "water", ounces, pts)
    total = await daily_points_for_user(interaction.user.id, today_str())

    await interaction.response.send_message(
        f"💧 Logged **{ounces} oz** of water for **{pts:.2f} pts**.\n"
        f"Your total for today is now **{total:.2f} pts**."
    )


@tree.command(name="log_alcohol", description="Log your alcohol intake (drinks).")
@app_commands.describe(
    drinks="Number of alcoholic drinks today."
)
async def log_alcohol(interaction: discord.Interaction, drinks: app_commands.Range[int, 0, 30]):
    pts = calc_alcohol_penalty(drinks)
    await add_log(interaction.user, today_str(), "alcohol", drinks, pts)
    total = await daily_points_for_user(interaction.user.id, today_str())

    await interaction.response.send_message(
        f"🍺 Logged **{drinks} drinks** for **{pts:.2f} pts** (negative is bad 😈).\n"
        f"Your total for today is now **{total:.2f} pts**."
    )


@tree.command(name="log_pastry", description="Log pastries/desserts eaten.")
@app_commands.describe(
    count="Number of pastries/desserts."
)
async def log_pastry(interaction: discord.Interaction, count: app_commands.Range[int, 0, 20]):
    pts = calc_pastry_penalty(count)
    await add_log(interaction.user, today_str(), "pastry", count, pts)
    total = await daily_points_for_user(interaction.user.id, today_str())

    await interaction.response.send_message(
        f"🥐 Logged **{count} pastries** for **{pts:.2f} pts** (negative).\n"
        f"Your total for today is now **{total:.2f} pts**."
    )


@tree.command(name="log_fastfood", description="Log fast-food meals eaten.")
@app_commands.describe(
    meals="Number of fast-food meals."
)
async def log_fastfood(interaction: discord.Interaction, meals: app_commands.Range[int, 0, 10]):
    pts = calc_fastfood_penalty(meals)
    await add_log(interaction.user, today_str(), "fastfood", meals, pts)
    total = await daily_points_for_user(interaction.user.id, today_str())

    await interaction.response.send_message(
        f"🍟 Logged **{meals} fast-food meals** for **{pts:.2f} pts** (negative).\n"
        f"Your total for today is now **{total:.2f} pts**."
    )


# -----------------------
# DAILY SUMMARY & LEADERBOARD
# -----------------------

@tree.command(name="daily_summary", description="See your point breakdown for today.")
async def daily_summary(interaction: discord.Interaction):
    date = today_str()
    total = await daily_points_for_user(interaction.user.id, date)
    breakdown = await daily_breakdown_for_user(interaction.user.id, date)

    if not breakdown:
        await interaction.response.send_message("You have no logs for today yet.")
        return

    lines = [f"📅 Summary for **{date}**:"]
    for category, pts in breakdown:
        lines.append(f"- **{category}**: {pts:.2f} pts")
    lines.append(f"\n**Total:** {total:.2f} pts")

    await interaction.response.send_message("\n".join(lines))


@tree.command(name="leaderboard", description="Show leaderboard for a date range (default: last 7 days).")
@app_commands.describe(
    days="How many days back from today? (Default 7)"
)
async def leaderboard_cmd(interaction: discord.Interaction, days: app_commands.Range[int, 1, 90] = 7):
    end = dt.datetime.utcnow().date()
    start = end - dt.timedelta(days=days - 1)
    start_str = start.strftime("%Y-%m-%d")
    end_str = end.strftime("%Y-%m-%d")

    rows = await leaderboard_for_range(start_str, end_str, limit=10)

    if not rows:
        await interaction.response.send_message("No logs found in that period.")
        return

    desc_lines = []
    for rank, (user_id, username, total_pts) in enumerate(rows, start=1):
        # get current streak for each leaderboard user (optional but fun)
        curr_streak, _ = await current_and_best_streak_for_user(user_id)
        badge = streak_badge(curr_streak) if curr_streak > 0 else "✨"
        desc_lines.append(
            f"**{rank}.** {username} — `{total_pts:.2f} pts` "
            f"(streak: {curr_streak}d, {badge})"
        )

    embed = discord.Embed(
        title=f"🏆 Leaderboard ({start_str} → {end_str})",
        description="\n".join(desc_lines),
        color=discord.Color.gold()
    )
    await interaction.response.send_message(embed=embed)


# -----------------------
# RULES
# -----------------------

@tree.command(name="rules", description="Show the fitness challenge rules.")
async def rules(interaction: discord.Interaction):
    text = (
        "📜 **Fitness Challenge Rules (Simplified)**\n\n"
        "**EARNING POINTS**\n"
        "• Lifting: 30m=1, 45m=1.25, 60m=1.5 (`/log_lift`)\n"
        "• Running/Cardio: 30m=1, 45m=1.25, 60m=1.5 (`/log_run`)\n"
        "• Steps: 10k=1, 15k+=1.5 (`/log_steps`)\n"
        "• Sleep: 6–7.9h=1, 8h+=2 (`/log_sleep`)\n"
        "• Protein: heavy meal=1, shake=0.5, cap 1.5 (`/log_protein`)\n"
        "• Supplements: +0.25 each (vitamins, creatine, magnesium, omega-3) (`/log_supplements`)\n"
        "• Water: 80oz+=0.5 (`/log_water`)\n\n"
        "**NEGATIVE POINTS**\n"
        "• Alcohol: -1 per 3 drinks (`/log_alcohol`)\n"
        "• Pastries: -1 each (`/log_pastry`)\n"
        "• Fast Food: -1 each (`/log_fastfood`)\n\n"
        "Use `/checkin`, `/yesterday_checkin`, `/week_summary`, `/weekly_winners`, `/streak`, "
        "`/daily_summary`, and `/leaderboard` to play."
    )
    await interaction.response.send_message(text)

async def today_leaderboard(limit: int = 10):
    """
    Convenience helper: leaderboard for *today* only.
    """
    d = today_str()
    return await leaderboard_for_range(d, d, limit=limit)

# -----------------------
# CHECK-IN COMMANDS
# -----------------------

@tree.command(name="checkin", description="Guided daily check-in for all metrics (today).")
async def checkin(interaction: discord.Interaction):
    date = today_str()
    await interaction.response.send_message(
        f"✅ Starting check-in for **{date}** (today). "
        "I'll ask you a few questions.\n"
        "You can answer with a number or type `skip` to use the default (usually 0)."
    )
    await run_checkin_dialog(interaction, date, label="today")


@tree.command(name="yesterday_checkin", description="Guided check-in for yesterday's metrics.")
async def yesterday_checkin(interaction: discord.Interaction):
    date = yesterday_str()
    await interaction.response.send_message(
        f"✅ Starting check-in for **{date}** (yesterday). "
        "I'll ask you a few questions.\n"
        "You can answer with a number or type `skip` to use the default (usually 0)."
    )
    await run_checkin_dialog(interaction, date, label="yesterday")


# -----------------------
# WEEK SUMMARY & STREAK
# -----------------------

@tree.command(name="week_summary", description="See your totals for the last N days (default 7).")
@app_commands.describe(
    days="How many days back from today? (Default 7, max 30)"
)
async def week_summary(interaction: discord.Interaction, days: app_commands.Range[int, 1, 30] = 7):
    end = dt.datetime.utcnow().date()
    start = end - dt.timedelta(days=days - 1)
    start_str = start.strftime("%Y-%m-%d")
    end_str = end.strftime("%Y-%m-%d")

    rows = await weekly_totals_for_user(interaction.user.id, start_str, end_str)

    if not rows:
        await interaction.response.send_message(
            f"No logs found for you between **{start_str}** and **{end_str}**."
        )
        return

    total_overall = 0.0
    lines = [f"📊 **{days}-day summary** for {interaction.user.mention} "
             f"({start_str} → {end_str}):"]
    for date, pts in rows:
        total_overall += pts
        lines.append(f"- `{date}`: **{pts:.2f} pts**")

    lines.append(f"\n**Total over {days} days:** `{total_overall:.2f} pts`")

    curr_streak, best_streak = await current_and_best_streak_for_user(interaction.user.id)
    badge = streak_badge(curr_streak)

    lines.append(
        f"\n🔥 **Current streak** (≥4 pts/day): `{curr_streak} days`\n"
        f"🏅 **Best streak (last 90 days)**: `{best_streak} days`\n"
        f"🎖️ **Badge:** {badge}"
    )

    await interaction.response.send_message("\n".join(lines))


@tree.command(name="streak", description="See your current and best streak plus badge.")
async def streak_cmd(interaction: discord.Interaction):
    curr_streak, best_streak = await current_and_best_streak_for_user(interaction.user.id)
    badge = streak_badge(curr_streak)

    await interaction.response.send_message(
        f"🔥 **Current streak** (≥4 pts/day): `{curr_streak} days`\n"
        f"🏅 **Best streak (last 90 days)**: `{best_streak} days`\n"
        f"🎖️ **Badge:** {badge}"
    )


# -----------------------
# WEEKLY WINNERS
# -----------------------

@tree.command(name="weekly_winners", description="Announce weekly winners for the last N days (default 7).")
@app_commands.describe(
    days="How many days back from today? (Default 7, max 30)",
    top_n="How many top players to show? (Default 3, max 10)"
)
async def weekly_winners(
    interaction: discord.Interaction,
    days: app_commands.Range[int, 1, 30] = 7,
    top_n: app_commands.Range[int, 1, 10] = 3
):
    end = dt.datetime.utcnow().date()
    start = end - dt.timedelta(days=days - 1)
    start_str = start.strftime("%Y-%m-%d")
    end_str = end.strftime("%Y-%m-%d")

    rows = await leaderboard_for_range(start_str, end_str, limit=top_n)

    if not rows:
        await interaction.response.send_message(
            f"No logs found between **{start_str}** and **{end_str}**."
        )
        return

    desc_lines = []
    winner_name = None
    winner_pts = None

    for rank, (user_id, username, total_pts) in enumerate(rows, start=1):
        if rank == 1:
            winner_name = username
            winner_pts = total_pts

        curr_streak, best_streak = await current_and_best_streak_for_user(user_id)
        badge = streak_badge(curr_streak)
        desc_lines.append(
            f"**#{rank}** {username} — `{total_pts:.2f} pts` "
            f"(streak: {curr_streak}d, best: {best_streak}d, {badge})"
        )

    title = f"🏆 Weekly Winners ({start_str} → {end_str})"
    description = "\n".join(desc_lines)

    embed = discord.Embed(
        title=title,
        description=description,
        color=discord.Color.green()
    )

    if winner_name is not None:
        embed.add_field(
            name="🥇 Champion",
            value=f"**{winner_name}** with `{winner_pts:.2f} pts`",
            inline=False
        )

    await interaction.response.send_message(
        content="📣 **Weekly Results Are In!**",
        embed=embed
    )

# -----------------------
# DAILY MOTIVATION & FACT DROPS
# -----------------------

import random

# 20 general motivation quotes
GENERAL_QUOTES = [
    "LFG!!! New day, new chance to not be mid. 💥",
    "You don’t need a mood, you need a mission. Get after it. 🎯",
    "Discipline is doing it when you don’t feel like it — and you don’t feel like it a LOT. Do it anyway.",
    "You’re one decision away from a completely different life. Make the hard one.",
    "Your future self is watching you right now, judging. Make them proud.",
    "Comfort is the enemy. Get uncomfortable, get better.",
    "If you’re waiting to ‘feel ready’, you’ll wait forever. Start dirty, fix it live.",
    "You either make progress or make excuses. Same energy, different results.",
    "Losers wait for motivation. Winners show up out of habit.",
    "Your problems won’t get lighter. You need to get stronger.",
    "Every time you don’t quit, you just insult your old lazy self. Keep insulting them.",
    "You’re not behind. You’re just earlier in the storyline. Keep grinding.",
    "Tired? Good. That’s the tax you pay for improvement.",
    "When you think you’re done, you’re probably at 40%. Push past it.",
    "If it scares you and it’s good for you — that’s exactly what you should be doing.",
    "You don’t rise to the level of your goals. You fall to the level of your systems.",
    "Success is just boredom done consistently. Boring work, legendary results.",
    "You’re not fragile. You’ve survived every bad day so far. Keep going.",
    "Talk less about the grind, grind more so people talk about you.",
    "You are building a version of you that your enemies will fear. Stay at it.",
]

# 30 fitness motivation quotes
FITNESS_QUOTES = [
    "Get your ass under the bar today. No excuses. 💪",
    "LET’S GET THIS BREAD 🍞 and then burn it off.",
    "Stop scrolling. Start lifting. The weights are waiting.",
    "Pain is temporary, looking powerful is a long-term investment.",
    "Sweat now, flex later. The pump is your signature.",
    "No one cares how tired you are. Hit the set.",
    "You don’t need motivation; you need a scheduled workout.",
    "Your gym membership doesn’t get you results. Showing up does.",
    "Your body is voting with every rep. Vote for strength.",
    "Cardio doesn’t kill gains. Skipping cardio kills your lungs.",
    "Every rep is a receipt that you were here and you worked.",
    "You can’t out-talk a bad physique. Train.",
    "Progress pics > excuses. Take both, compare.",
    "Your warm-up is someone else’s max. Respect it and get better.",
    "You won’t remember being tired. You will remember quitting.",
    "If you still look cute at the end, you didn’t work hard enough.",
    "Leg day builds humility and domination at the same time.",
    "You don’t ‘find’ time to train. You steal it from weaker versions of yourself.",
    "Slow progress is still progress. Fast excuses are still excuses.",
    "The iron is honest. It doesn’t care who you are, only what you do.",
    "No magical program beats ‘show up 4–6 days a week’ for a year.",
    "A bad workout is better than the perfect workout you never did.",
    "The first set is negotiation. The last set is war.",
    "You’re one hard month away from being unrecognizable. Start the month.",
    "Stop saying ‘I’ll start Monday’. Today is a perfectly good day to suffer.",
    "Muscle doesn’t grow from comfort. It grows from ‘damn, that was heavy.’",
    "You’re not too old, too busy, or too tired. You’re under-trained.",
    "You’re building armor. Not just for your body, for your life.",
    "Your workout is therapy with receipts. Pay the bill.",
    "Be the strongest one in your friend group. Set the standard.",
]

# 30 knight / honor / code motivation quotes
KNIGHT_QUOTES = [
    "A knight’s first armor is discipline; steel just covers the outside.",
    "Honor is when your actions match your code, even when nobody’s watching.",
    "The weak wait for the perfect moment; the knight sharpens his blade every day.",
    "You do not rise to the occasion; you fall to the level of your training.",
    "A code is not a slogan. It’s the rules you obey when it hurts.",
    "The enemy is rarely out there. It’s the coward inside you.",
    "A true knight keeps his word, his blade sharp, and his heart steady.",
    "Every rep is a hammer blow on the armor of your character.",
    "A man without a code is a sword without a wielder — dangerous, but pointless.",
    "The day you stop training is the day your shield starts to crack.",
    "Knighthood isn’t a title; it’s the weight of responsibility you choose to carry.",
    "A warrior doesn’t hope for an easier path; he sharpens himself for the hard one.",
    "Strength without honor is just intimidation. Honor without strength is just a wish.",
    "Your past failures are dents in your armor, not reasons to stop fighting.",
    "When the village sleeps, the knight keeps watch. When they doubt, he keeps training.",
    "You don’t swear an oath once; you renew it with every hard choice.",
    "Cowards break when it’s heavy. Knights grip tighter.",
    "The code is simple: protect, improve, endure, repeat.",
    "It’s not about winning one battle; it’s about being ready for all of them.",
    "In the gym and in life, steel is tested in fire, not in comfort.",
    "Your integrity is the sword you take into every room.",
    "A knight respects the weight, but never fears it.",
    "You can’t wear honor like a cloak; it’s carved into your bones by your choices.",
    "The smallest promise to yourself is still sacred. Keep it.",
    "You don’t have to be the strongest knight, just the one who never stops advancing.",
    "Your code is written in your habits, not your words.",
    "Any fool can lift when it’s easy. The knight lifts when it’s dark and cold.",
    "You don’t need a crown. You need a standard you refuse to drop.",
    "A real knight doesn’t seek comfort. He seeks capability.",
    "Protect your people. Perfect your craft. Guard your mind. That is the path.",
]

# 30 fitness facts
FITNESS_FACTS = [
    "Fact: Consistent strength training 2–3 times per week can significantly increase muscle mass and bone density over time.",
    "Fact: NEAT (non-exercise activity thermogenesis) — walking, fidgeting, stairs — can burn more calories per day than your actual workout.",
    "Fact: Muscles don’t grow in the gym; they grow during rest and recovery, especially sleep.",
    "Fact: Heavy compound lifts like squats and deadlifts stimulate more muscle and hormone response than machine-only workouts.",
    "Fact: Cardio improves heart health but also helps with recovery by increasing blood flow to your muscles.",
    "Fact: You can gain strength even in a calorie deficit, especially as a beginner.",
    "Fact: DOMS (delayed onset muscle soreness) is not a perfect indicator of progress; you can grow without being wrecked every session.",
    "Fact: Progressive overload — increasing weight, reps, or difficulty over time — is the core driver of muscle growth.",
    "Fact: Flexibility and mobility work can reduce injury risk and actually improve strength performance.",
    "Fact: Working out with friends or a group massively increases adherence and enjoyment.",
    "Fact: Even 10–15 minutes of daily movement is better than zero and improves health markers.",
    "Fact: HIIT (high-intensity interval training) can improve cardiovascular fitness with shorter time commitments.",
    "Fact: Grip strength is correlated with overall health and longevity in many studies.",
    "Fact: A strong posterior chain (back, glutes, hamstrings) helps posture, athleticism, and reduces back pain.",
    "Fact: Consistent training changes not just your muscles, but your brain’s motor patterns and coordination.",
    "Fact: Walking is one of the most underrated fat-loss tools — low stress, repeatable, and sustainable.",
    "Fact: Lifting weights can improve insulin sensitivity and blood sugar control.",
    "Fact: Balance and stability training reduce the risk of falls and injuries, especially as you age.",
    "Fact: Even short “exercise snacks” (5 minutes of movement a few times a day) contribute to better health.",
    "Fact: Muscle is metabolically active tissue; more muscle generally means a higher resting metabolic rate.",
    "Fact: Training close to failure (with good form) is more important than fancy exercises for growth.",
    "Fact: Good form under lighter weight beats ugly reps with ego weight every time.",
    "Fact: Consistent training can reduce symptoms of anxiety and depression for many people.",
    "Fact: Periodizing your training (cycles of intensity/volume) can prevent plateaus and burnout.",
    "Fact: Strength training supports joint health by strengthening the muscles and tissues around them.",
    "Fact: You can maintain most of your gains with far less volume than it took to build them.",
    "Fact: Cardio and lifting together create better health outcomes than either alone for most people.",
    "Fact: Training your core is about stability and bracing, not just endless crunches.",
    "Fact: The best program is the one you can stick to consistently for months and years.",
    "Fact: It’s never “too late” to start; people build strength and muscle well into their 60s and beyond.",
]

# 30 brain health facts
BRAIN_FACTS = [
    "Fact: Regular aerobic exercise increases blood flow to the brain and is linked to better memory and learning.",
    "Fact: Quality sleep is when your brain consolidates memories and clears metabolic waste.",
    "Fact: Chronic stress can physically shrink areas of the brain like the hippocampus if unmanaged.",
    "Fact: Strength training has been associated with better cognitive function in older adults.",
    "Fact: Learning new skills (languages, instruments, complex games) builds new neural connections.",
    "Fact: Social connection is a powerful protector against cognitive decline and depression.",
    "Fact: Omega-3 fatty acids (like DHA) are important structural components of brain cell membranes.",
    "Fact: Dehydration as little as 1–2% can negatively affect focus, mood, and reaction time.",
    "Fact: Excessive alcohol intake can damage brain cells and impair memory over time.",
    "Fact: Meditation and mindfulness practices can change brain structure (like thickening the prefrontal cortex).",
    "Fact: Regular physical exercise is one of the strongest lifestyle tools to reduce risk of dementia.",
    "Fact: The brain uses about 20% of the body’s resting energy despite being only ~2% of body weight.",
    "Fact: Good cardiovascular health is closely tied to brain health — what’s good for the heart is often good for the brain.",
    "Fact: Chronic sleep deprivation impairs decision-making, reaction time, and emotional regulation.",
    "Fact: Learning and exercising together (like complex sports) are especially powerful for brain health.",
    "Fact: Vitamin B12 deficiency can lead to memory problems and neurological symptoms.",
    "Fact: High-sugar diets over time may negatively affect cognitive function and mood.",
    "Fact: Regular reading and mentally challenging activities help build cognitive reserve.",
    "Fact: Exposure to nature and sunlight can improve mood and cognitive performance.",
    "Fact: The brain is plastic — it can change structure and function throughout life with training and habit.",
    "Fact: Resistance training can increase levels of brain-derived neurotrophic factor (BDNF), which supports neuron growth.",
    "Fact: Poor mental health can show up as physical symptoms like fatigue and pain.",
    "Fact: Good gut health may be linked to better brain health via the gut-brain axis.",
    "Fact: Music training in childhood and adulthood is linked to better auditory and cognitive skills.",
    "Fact: Multi-tasking is often just rapid task-switching; deep focus is more efficient for complex work.",
    "Fact: Chronic exposure to screens late at night can interfere with melatonin and sleep quality.",
    "Fact: Properly managed stress (e.g., through exercise and breathing) can build resilience, not just exhaustion.",
    "Fact: Regular movement breaks during long work sessions improve attention and reduce mental fatigue.",
    "Fact: Creative activities (drawing, writing, building) stimulate multiple regions of the brain at once.",
    "Fact: A combination of diet, exercise, sleep, and social connection forms the foundation of long-term brain health.",
]

# 30 fitness nutrition / vitamin facts
NUTRITION_FACTS = [
    "Fact: Protein is the most satiating macronutrient and is crucial for muscle repair and growth.",
    "Fact: Most lifters benefit from roughly 0.7–1.0 grams of protein per pound of bodyweight per day, depending on goals.",
    "Fact: Creatine is one of the most researched supplements and is generally safe for healthy individuals.",
    "Fact: Vitamin D deficiency is common and can affect mood, bone health, and immune function.",
    "Fact: Magnesium plays a role in hundreds of enzymatic reactions, including muscle and nerve function.",
    "Fact: Omega-3 fatty acids may support heart health, brain function, and help modulate inflammation.",
    "Fact: Carbohydrates around workouts can support performance and recovery by replenishing glycogen.",
    "Fact: Hydration affects strength, endurance, and cognitive performance — even mild dehydration hurts performance.",
    "Fact: Fiber supports gut health, blood sugar control, and satiety; most people eat too little of it.",
    "Fact: Whole foods tend to provide more micronutrients than highly processed foods with the same calories.",
    "Fact: Chronic extreme calorie restriction can slow metabolism and lead to muscle loss.",
    "Fact: A small calorie surplus plus resistance training is typically best for lean muscle gain.",
    "Fact: A moderate calorie deficit, adequate protein, and lifting is best for fat loss with muscle retention.",
    "Fact: Alcohol provides 7 calories per gram and can interfere with recovery and sleep quality.",
    "Fact: Electrolytes like sodium, potassium, and magnesium are crucial for muscle contraction and nerve signaling.",
    "Fact: Caffeine can improve performance and focus, but too much can hurt sleep and recovery.",
    "Fact: Spreading protein intake across 3–4 meals may optimize muscle protein synthesis.",
    "Fact: Vitamin C plays a key role in collagen formation, important for skin, joints, and connective tissue.",
    "Fact: Calcium and vitamin D work together for bone health and strength.",
    "Fact: Highly processed, hyper-palatable foods are engineered to override normal hunger signals.",
    "Fact: Eating enough micronutrients (vitamins and minerals) supports hormone production and energy levels.",
    "Fact: Meal timing matters less than total daily calories and macros for body composition, for most people.",
    "Fact: Creatine might also support brain function and cognitive performance in addition to strength.",
    "Fact: Consistent, balanced nutrition beats hardcore short-term diets over the long run.",
    "Fact: A high-protein breakfast can reduce cravings and snacking later in the day.",
    "Fact: Omega-3 and omega-6 fatty acid balance matters; many people consume too much omega-6.",
    "Fact: Plant-based diets can support athletic performance if protein and key nutrients are planned well.",
    "Fact: Vitamins and supplements are helpers, not substitutes for a solid diet.",
    "Fact: Eating slowly and mindfully helps your brain register fullness more accurately.",
    "Fact: Good nutrition is a performance multiplier in the gym, not just a way to change the scale.",
]

# 30 Reconquista-related facts (presented historically / informationally)
RECONQUISTA_FACTS = [
    "Fact: The Reconquista refers to the centuries-long process (roughly 8th to 15th century) in which Christian kingdoms in Iberia expanded southward over territories controlled by Muslim states.",
    "Fact: The Muslim conquest of most of the Iberian Peninsula began around 711 CE, after the Battle of Guadalete.",
    "Fact: One early Christian stronghold was the Kingdom of Asturias in the north, associated with the Battle of Covadonga (traditionally dated 722 CE).",
    "Fact: Over time, northern Christian polities like Asturias evolved into later kingdoms such as León and Castile.",
    "Fact: The Kingdom of Navarre was another important Christian realm during the medieval history of Iberia.",
    "Fact: The Crown of Aragon, centered in the northeast, played a major role in Mediterranean politics and in later stages of the Reconquista.",
    "Fact: Portugal emerged as an independent kingdom in the 12th century and completed its own territorial expansion southward by the mid-13th century.",
    "Fact: The Almoravid and Almohad dynasties were powerful North African Muslim dynasties that ruled parts of Iberia and contested Christian advances.",
    "Fact: The Battle of Las Navas de Tolosa in 1212 was a key victory for several Christian kingdoms against Almohad forces.",
    "Fact: By the late 13th century, most of Iberia north of the Emirate of Granada was under Christian rule.",
    "Fact: The Emirate of Granada remained the last major Muslim-ruled territory in Iberia into the 15th century.",
    "Fact: The marriage of Ferdinand II of Aragon and Isabella I of Castile in the late 15th century unified two major Christian crowns.",
    "Fact: The final campaign against the Emirate of Granada took place in the 1480s and early 1490s.",
    "Fact: The capture of Granada in 1492 is often taken as a symbolic end point of the Reconquista.",
    "Fact: The Alhambra in Granada is a famous palace-fortress complex built under Nasrid rule prior to the conquest.",
    "Fact: Rodrigo Díaz de Vivar, known as ‘El Cid’, is a historical figure and later legendary hero in Castilian literature, connected to frontier warfare in Iberia.",
    "Fact: The Reconquista period involved alliances and conflicts that were not always strictly along religious lines.",
    "Fact: Frontier regions often had mixed populations with complex cultural, linguistic, and legal arrangements.",
    "Fact: Military orders (such as the Order of Santiago or Calatrava) participated in campaigns during the Reconquista era.",
    "Fact: Many medieval Iberian cities show layered architectural influences from Roman, Islamic, and Christian building traditions.",
    "Fact: Legal codes like the ‘Siete Partidas’ in Castile were shaped during and after periods of expansion and consolidation.",
    "Fact: The Reconquista era intersected with broader Mediterranean politics involving North Africa, France, and Italian states.",
    "Fact: After 1492, the unified crowns of Castile and Aragon turned increasing attention to Atlantic exploration.",
    "Fact: 1492 was also the year the Alhambra Decree ordered the expulsion of many Jews from the kingdoms of Castile and Aragon.",
    "Fact: The term ‘Reconquista’ itself became more widely used and symbolically charged in later centuries.",
    "Fact: Religious, political, and cultural narratives about the Reconquista have been interpreted differently over time by various groups.",
    "Fact: Many modern Spanish towns and festivals still commemorate events or figures associated with medieval Iberian conflicts.",
    "Fact: Scholarship on the period emphasizes both warfare and long-term cultural exchange across religious and linguistic boundaries.",
    "Fact: The history of medieval Iberia is studied today for its complex interactions among Christian, Muslim, and Jewish communities.",
    "Fact: Understanding the Reconquista involves examining both military events and the everyday lives of people on all sides of the frontier.",
]

def pick_general_channel(guild: discord.Guild) -> discord.abc.Messageable | None:
    """
    Try to find a good channel to send daily quotes/facts in:
    1) A channel named 'general'
    2) The system channel
    3) The first text channel the bot can send messages in
    """
    # 1) Prefer #general
    for channel in guild.text_channels:
        if channel.name.lower() == "general" and channel.permissions_for(guild.me).send_messages:
            return channel

    # 2) System channel
    if guild.system_channel and guild.system_channel.permissions_for(guild.me).send_messages:
        return guild.system_channel

    # 3) First available text channel
    for channel in guild.text_channels:
        if channel.permissions_for(guild.me).send_messages:
            return channel

    return None


def pick_random_member(guild: discord.Guild) -> discord.Member | None:
    """
    Pick a random human member (no bots).
    """
    humans = [m for m in guild.members if not m.bot]
    if not humans:
        return None
    return random.choice(humans)


def choose_random_drop() -> str:
    """
    Pick a random string from all categories.
    """
    pools = [
        GENERAL_QUOTES,
        FITNESS_QUOTES,
        KNIGHT_QUOTES,
        FITNESS_FACTS,
        BRAIN_FACTS,
        NUTRITION_FACTS,
        RECONQUISTA_FACTS,
    ]
    chosen_pool = random.choice(pools)
    return random.choice(chosen_pool)

@tree.command(name="quote", description="Drop a random motivational quote or fact and tag someone to hype them up!")
async def quote_command(interaction: discord.Interaction):
    guild = interaction.guild
    if guild is None:
        await interaction.response.send_message("This command only works in a server.")
        return

    # Pick a random human member to tag
    member = pick_random_member(guild)

    # Pick a random message (from all quote/fact pools)
    message = choose_random_drop()

    if member:
        msg = f"{member.mention} {message} 🏋️"
    else:
        msg = f"{message} 🏋️"

    await interaction.response.send_message(msg)

# -----------------------
# DAILY DROPS TASK (7 AM and 4 PM)
# -----------------------

DAILY_DROP_TIMES = [dt.time(hour=7, minute=0), dt.time(hour=16, minute=0)]
DAILY_REMINDER_CACHE: dict[tuple[int, int], str] = {}

@tasks.loop(time=DAILY_DROP_TIMES)
async def daily_drops_task():
    # Runs twice per day at 7:00 and 16:00 (server local time)
    print("Running daily_drops_task...")
    for guild in bot.guilds:
        channel = pick_general_channel(guild)
        if channel is None:
            continue

        member = pick_random_member(guild)
        content = choose_random_drop()

        if member:
            msg = f"{member.mention} {content} 🏋️"
        else:
            msg = f"{content} 🏋️"

        try:
            await channel.send(msg)
        except Exception as e:
            print(f"Failed to send daily drop in guild {guild.id}: {e}")


@daily_drops_task.before_loop
async def before_daily_drops():
    print("Waiting for bot to be ready before starting daily_drops_task...")
    await bot.wait_until_ready()

@bot.event
async def on_message(message: discord.Message):
    """
    When a real user speaks in the server, gently remind them once per day
    to log their activities and show today's scores.
    """
    # Always let other bot logic run
    await bot.process_commands(message)

    # Ignore DMs and bot messages
    if message.guild is None or message.author.bot:
        return

    guild = message.guild
    user = message.author

    # Only do this in certain channels (like #general or #fitness)
    channel_name = message.channel.name.lower()
    allowed_channels = {"general", "fitness", "gym", "fit-challenge"}

    if channel_name not in allowed_channels:
        return

    today = today_str()
    key = (guild.id, user.id)

    # Already reminded this user today in this guild? Skip
    if DAILY_REMINDER_CACHE.get(key) == today:
        return

    # Mark as reminded for today
    DAILY_REMINDER_CACHE[key] = today

    # Get this user's total and today's leaderboard
    user_total = await daily_points_for_user(user.id, today)
    rows = await today_leaderboard(limit=10)

    # Build a quick scoreboard
    if rows:
        lines = [f"📊 **Today’s scores ({today}):**"]
        for rank, (u_id, username, total_pts) in enumerate(rows, start=1):
            prefix = "👉" if u_id == user.id else f"{rank}."
            lines.append(f"{prefix} `{username}` — **{total_pts:.2f} pts**")
        leaderboard_text = "\n".join(lines)
    else:
        leaderboard_text = "No one has logged anything yet today. Be the first to start the grind. 💪"

    # Compose reminder message
    msg_lines = [
        f"{user.mention} welcome back, warrior. 🛡️",
        f"Your total for **today** so far: **{user_total:.2f} pts**.",
        "",
        "Don’t forget to log your activities:",
        "• `/checkin` for the full daily questionnaire",
        "• `/log_lift`, `/log_run`, `/log_steps`, `/log_sleep`, `/log_protein`, etc.",
        "",
        leaderboard_text,
    ]

    try:
        await message.channel.send("\n".join(msg_lines))
    except Exception as e:
        print(f"Failed to send auto-reminder in guild {guild.id}: {e}")

# -----------------------
# RUN
# -----------------------

if __name__ == "__main__":
    if not TOKEN:
        raise RuntimeError("DISCORD_TOKEN not set in .env")
    bot.run(TOKEN)
