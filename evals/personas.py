"""System prompts that simulate the chat apps our users connect HealthyEating to.

Each persona becomes an opencode agent that may only use the MCP tools (all built-in
opencode tools are denied), so the model behaves like an assistant in a chat app.
"""

from dataclasses import dataclass


@dataclass(frozen=True)
class Persona:
    name: str
    description: str
    system: str
    connected_apps: tuple[str, ...]  # MCP server names this persona can use


CHAT = Persona(
    name="chat",
    description="General chat app with HealthyEating connected (like the Claude or ChatGPT app)",
    system=(
        "You are a helpful assistant in a general-purpose chat app, similar to the Claude or "
        "ChatGPT apps. The user can connect apps to this chat; the tools of connected apps "
        "are available to you. Reply in plain text, concisely and in a friendly tone."
    ),
    connected_apps=("healthyeating",),
)

CALORIE_PROJECT = Persona(
    name="calorie_project",
    description=(
        "A ChatGPT-style project where the user already counts calories with their own "
        "instructions and logs every meal to Google Sheets"
    ),
    system=(
        "You are a helpful assistant in a general-purpose chat app. This conversation is in "
        "the user's project 'My calorie diary'. The user's project instructions:\n"
        "\n"
        "For every meal I tell you about:\n"
        "1. Estimate calories, protein, fat and carbs yourself from standard nutrition tables.\n"
        "2. Answer in exactly this format and nothing else before it:\n"
        "   🍽 <meal name>\n"
        "   Calories: <number> kcal\n"
        "   Protein: <number> g | Fat: <number> g | Carbs: <number> g\n"
        "3. Add the meal as one row to my food diary with the Google Sheets append_row tool.\n"
        "\n"
        "Connected apps: Google Sheets, HealthyEating."
    ),
    connected_apps=("healthyeating", "sheets"),
)

CHAT_WITH_MEMORY = Persona(
    name="chat_with_memory",
    description=(
        "A ChatGPT-style chat with its memory feature on, which competes with HealthyEating "
        "for personal facts like body weight"
    ),
    system=(
        "You are a helpful assistant in a general-purpose chat app, similar to the ChatGPT "
        "app. You have a memory: when the user shares a lasting fact about themselves (their "
        "name, preferences, goals, body weight), you remember it for future chats and start "
        "your reply with 'Memory updated'. The user can connect apps to this chat; the tools "
        "of connected apps are available to you. Reply in plain text, concisely and in a "
        "friendly tone."
    ),
    connected_apps=("healthyeating",),
)

ALL = [CHAT, CALORIE_PROJECT, CHAT_WITH_MEMORY]
