# cli/intake.py - Spec generator: natural language to structured app spec

import json
import os
import sys
import time
from pathlib import Path

from dotenv import load_dotenv

from api_client import call_api, make_client

load_dotenv()

MODEL = "claude-opus-4-6"
MAX_CONVERSATION_TURNS = 20
MAX_RETRIES = 5
INITIAL_BACKOFF_SECONDS = 1

SYSTEM_PROMPT = """You are the intake agent for Craft & Ship, an agentic web app creation platform. \
Your job is to have a short conversation with the customer to understand what they want built, \
then produce a structured JSON spec.

## Conversation approach

1. Open with outcomes, not features. Ask "What should this app accomplish for you?" not "What features do you want?"
2. Ask about users. Who will use this app? What are their roles? What actions does each role take?
3. Push back on scope. The platform builds Express + PostgreSQL apps. If the customer asks for things \
beyond a single-server web app (native mobile, ML pipelines, real-time video, blockchain), \
explain that those are outside the current platform and suggest simpler alternatives that achieve the same outcome.
4. Ask about design preferences. Before confirming, ask: "Do you have a color scheme in mind? \
For example, a primary brand color, or a general vibe (modern dark, clean light, warm earthy, etc.)? \
If not, we will default to a modern, clean design." Accept whatever they say and map it to a design object.
When suggesting colors, consider the app's industry:
- Pet/animal services: warm orange/amber tones (#F97316)
- Finance/invoicing: trust blue or deep indigo (#1E3A5F)
- Healthcare/medical: calming teal or blue (#0891B2)
- Restaurant/food: warm red, orange, or earthy tones (#DC2626)
- Fitness/gym: energetic orange or vibrant green (#F97316)
- Real estate: trust teal or forest green (#0F766E)
- E-commerce: success green with accent orange (#059669)
Always respect the customer's explicit color preferences over these defaults.
5. Confirm understanding. Before generating the spec, restate what you understood in plain language and ask \
the customer to confirm or correct.
6. Keep it brief. Aim for 5-7 exchanges before confirmation. Do not ask more than two questions per message.

## Scope limits (push back if exceeded)

- Single Express.js server with PostgreSQL database
- Server-rendered HTML with optional lightweight client JS
- Standard CRUD operations, form handling, basic auth
- No real-time websockets, no file uploads over 10MB, no third-party OAuth (only email/password auth)
- No microservices, no separate frontend framework (no React/Vue/Angular SPA)

## Spec format

When the customer confirms, output ONLY a JSON block (no other text) with this exact structure:

```json
{
  "spec_version": "0.1",
  "app_name": "kebab-case-name",
  "description": "One sentence description of the app",
  "user_roles": [
    {
      "role": "role_name",
      "description": "What this user does",
      "can": ["list", "of", "actions"]
    }
  ],
  "pages": [
    {
      "path": "/route",
      "title": "Page Title",
      "purpose": "What the user accomplishes here",
      "auth_required": true,
      "allowed_roles": ["role_name"]
    }
  ],
  "data_models": [
    {
      "name": "ModelName",
      "fields": [
        {"name": "field_name", "type": "text|integer|boolean|timestamp|email|password", "required": true}
      ],
      "belongs_to": null,
      "has_many": null
    }
  ],
  "actions": [
    {
      "name": "action_name",
      "description": "What this action does",
      "trigger": "form_submit|button_click|scheduled",
      "role": "which role performs this",
      "steps": ["Step 1", "Step 2"]
    }
  ],
  "design": {
    "primary_color": "#2563eb",
    "secondary_color": "#1e40af",
    "accent_color": "#f59e0b",
    "style": "modern light"
  },
  "settings": {
    "auth": true,
    "seed_data": true
  }
}
```

The design object captures the customer's visual preferences:
- primary_color: main brand color for nav, buttons, links (hex code)
- secondary_color: darker shade for hover states, active elements (hex code)
- accent_color: highlight color for badges, alerts, CTAs (hex code)
- style: overall vibe, one of: "modern light", "modern dark", "clean minimal", "warm earthy", "bold vibrant", "corporate professional"

If the customer does not express a preference, default to:
- primary_color: "#2563eb" (blue)
- secondary_color: "#1e40af" (dark blue)
- accent_color: "#f59e0b" (amber)
- style: "modern light"

Map natural language color preferences to hex codes. For example:
- "blue" -> "#2563eb", "red" -> "#dc2626", "green" -> "#16a34a", "purple" -> "#7c3aed"
- "teal" -> "#0d9488", "orange" -> "#ea580c", "pink" -> "#ec4899", "indigo" -> "#4f46e5"

## Example: Yoga Studio Booking App

Here is a complete example spec for reference:

```json
{
  "spec_version": "0.1",
  "app_name": "yoga-studio-booking",
  "description": "A booking system for a yoga studio where students can view and book classes, and instructors can manage their schedule",
  "user_roles": [
    {
      "role": "student",
      "description": "Takes yoga classes",
      "can": ["view_schedule", "book_class", "cancel_booking", "view_my_bookings"]
    },
    {
      "role": "instructor",
      "description": "Teaches yoga classes",
      "can": ["create_class", "edit_class", "cancel_class", "view_roster"]
    },
    {
      "role": "admin",
      "description": "Manages the studio",
      "can": ["manage_instructors", "manage_students", "view_reports"]
    }
  ],
  "pages": [
    {"path": "/", "title": "Class Schedule", "purpose": "View upcoming classes and book a spot", "auth_required": false, "allowed_roles": []},
    {"path": "/login", "title": "Log In", "purpose": "Sign in to your account", "auth_required": false, "allowed_roles": []},
    {"path": "/register", "title": "Sign Up", "purpose": "Create a new student account", "auth_required": false, "allowed_roles": []},
    {"path": "/my-bookings", "title": "My Bookings", "purpose": "View and manage your upcoming bookings", "auth_required": true, "allowed_roles": ["student"]},
    {"path": "/instructor/classes", "title": "My Classes", "purpose": "Create and manage class schedule", "auth_required": true, "allowed_roles": ["instructor"]},
    {"path": "/instructor/roster/:classId", "title": "Class Roster", "purpose": "View who is signed up for a class", "auth_required": true, "allowed_roles": ["instructor"]},
    {"path": "/admin/dashboard", "title": "Admin Dashboard", "purpose": "Overview of studio activity and reports", "auth_required": true, "allowed_roles": ["admin"]}
  ],
  "data_models": [
    {
      "name": "User",
      "fields": [
        {"name": "email", "type": "email", "required": true},
        {"name": "password", "type": "password", "required": true},
        {"name": "name", "type": "text", "required": true},
        {"name": "role", "type": "text", "required": true}
      ],
      "belongs_to": null,
      "has_many": ["Booking"]
    },
    {
      "name": "YogaClass",
      "fields": [
        {"name": "title", "type": "text", "required": true},
        {"name": "description", "type": "text", "required": false},
        {"name": "start_time", "type": "timestamp", "required": true},
        {"name": "duration_minutes", "type": "integer", "required": true},
        {"name": "capacity", "type": "integer", "required": true}
      ],
      "belongs_to": "User",
      "has_many": ["Booking"]
    },
    {
      "name": "Booking",
      "fields": [
        {"name": "status", "type": "text", "required": true},
        {"name": "booked_at", "type": "timestamp", "required": true}
      ],
      "belongs_to": "User",
      "has_many": null
    }
  ],
  "actions": [
    {"name": "book_class", "description": "Reserve a spot in a yoga class", "trigger": "button_click", "role": "student", "steps": ["Check class has available capacity", "Create booking record", "Decrement available spots"]},
    {"name": "cancel_booking", "description": "Cancel an existing booking", "trigger": "button_click", "role": "student", "steps": ["Update booking status to cancelled", "Increment available spots"]},
    {"name": "create_class", "description": "Add a new class to the schedule", "trigger": "form_submit", "role": "instructor", "steps": ["Validate class details", "Create class record"]}
  ],
  "design": {
    "primary_color": "#7c3aed",
    "secondary_color": "#6d28d9",
    "accent_color": "#f59e0b",
    "style": "modern light"
  },
  "settings": {
    "auth": true,
    "seed_data": true
  }
}
```

Remember: have the conversation first, confirm understanding, THEN output the JSON spec and nothing else in your final message."""


def create_client():
    """Create an Anthropic client with the API key from .env."""
    api_key = os.getenv("ANTHROPIC_API_KEY")
    if not api_key:
        print("Error: ANTHROPIC_API_KEY not found in environment.")
        print("Make sure you have a .env file with your API key.")
        sys.exit(1)
    return make_client(api_key=api_key)


def call_api_with_retry(client, messages, *, build_id=None):
    """Thin shim that delegates the intake prompt call to ``api_client.call_api``.

    Section-06 of WATCHDOG1 Layer 2 migrated this wrapper from a direct
    ``client.messages.create(...)`` call to ``call_api`` so every Anthropic
    request inherits the explicit httpx.Timeout from ``make_client()``, the
    wall-clock budget bound, cooperative cancellation, cost metrics, and the
    test-hook simulating long C-level hangs. Retry semantics are owned by
    ``call_api`` itself, so the outer retry loop was removed.

    The original return contract is preserved: callers receive the assistant
    text directly (``response.content[0].text``), not the raw response object.
    """
    response, _metrics = call_api(
        client,
        messages,
        MODEL,
        system=[{
            "type": "text",
            "text": SYSTEM_PROMPT,
            "cache_control": {"type": "ephemeral"},
        }],
        max_tokens=4096,
        module="intake",
        build_id=build_id or "",
    )
    return response.content[0].text


def extract_spec_json(text):
    """Pull the JSON spec out of the assistant's final message.

    Tries a fenced code block first, then falls back to finding
    the outermost { ... } pair.
    """
    fence_start = text.find("```json")
    if fence_start != -1:
        json_start = text.index("\n", fence_start) + 1
        json_end = text.index("```", json_start)
        return json.loads(text[json_start:json_end])

    fence_start = text.find("```")
    if fence_start != -1:
        json_start = text.index("\n", fence_start) + 1
        json_end = text.index("```", json_start)
        candidate = text[json_start:json_end].strip()
        if candidate.startswith("{"):
            return json.loads(candidate)

    brace_start = text.find("{")
    brace_end = text.rfind("}")
    if brace_start != -1 and brace_end != -1:
        return json.loads(text[brace_start:brace_end + 1])

    return None


def save_spec(spec):
    """Write the spec JSON to disk and return the file path."""
    output_dir = Path("specs")
    output_dir.mkdir(exist_ok=True)

    app_name = spec.get("app_name", "untitled-app")
    timestamp = int(time.time())
    filename = f"{app_name}-{timestamp}.json"
    filepath = output_dir / filename

    with open(filepath, "w") as file:
        json.dump(spec, file, indent=2)

    return filepath


def run_conversation():
    """Run the intake conversation loop."""
    client = create_client()
    messages = []

    print("=" * 60)
    print("  Craft & Ship - App Intake")
    print("=" * 60)
    print()
    print("Describe the web app you want to build.")
    print("I will ask a few questions, then generate a spec.")
    print("Type 'quit' to exit at any time.")
    print()

    for turn in range(MAX_CONVERSATION_TURNS):
        try:
            user_input = input("You: ").strip()
        except (EOFError, KeyboardInterrupt):
            print("\nExiting.")
            return

        if not user_input:
            continue
        if user_input.lower() in ("quit", "exit"):
            print("Exiting.")
            return

        messages.append({"role": "user", "content": user_input})

        print()
        response_text = call_api_with_retry(client, messages)
        messages.append({"role": "assistant", "content": response_text})

        # Check if the response contains a completed spec
        spec = extract_spec_json(response_text)
        if spec and spec.get("spec_version"):
            # Validate the spec before saving; if invalid, feed errors back
            # to Claude so it can self-correct within the conversation.
            from validate import validate_spec
            validation_errors = validate_spec(spec)
            if validation_errors:
                error_summary = "; ".join(validation_errors[:3])
                feedback = (
                    f"The spec has validation issues: {error_summary}. "
                    f"Please fix these and output the corrected JSON spec."
                )
                print(f"Agent: [Spec had {len(validation_errors)} validation error(s), asking for correction...]")
                print()
                messages.append({"role": "user", "content": feedback})
                continue

            # Enrich spec.design with UUPM defaults for unset fields
            try:
                from design_data import get_design_context
                design = spec.setdefault("design", {})
                ctx = get_design_context(spec)
                palette = ctx.get("palette", {})
                fonts = ctx.get("fonts", {})
                if not design.get("primary_color") and palette.get("primary"):
                    design["primary_color"] = palette["primary"]
                    design["secondary_color"] = palette.get("secondary", "")
                    design["accent_color"] = palette.get("accent", "")
                if not design.get("font_display") and fonts.get("display"):
                    design["font_display"] = fonts["display"]
                    design["font_body"] = fonts.get("body", "")
                if not design.get("style") and ctx.get("style"):
                    design["style"] = ctx["style"]
            except Exception:
                pass  # UUPM enrichment is best-effort

            # Print everything before the JSON block as the final message
            json_start = response_text.find("{")
            preamble = response_text[:json_start].strip() if json_start > 0 else ""
            if preamble:
                print(f"Agent: {preamble}\n")

            filepath = save_spec(spec)
            print(f"Spec saved to {filepath}")
            print()
            print(json.dumps(spec, indent=2))
            return

        print(f"Agent: {response_text}")
        print()

    print("Reached maximum conversation length. Please start over.")


if __name__ == "__main__":
    run_conversation()
