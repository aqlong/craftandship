# coordinator/sample_build_specs.py: sample app specs the coordinator can queue
# as real builds, to exercise the build pipeline end to end.
#
# This is data plus two lookups, not a test module (it was once named
# test_build_specs.py and holds no tests). It is used on one path:
# with ENABLE_TEST_BUILDS=true (off by default), work detection's
# _check_test_build_needed emits a `test_build` item when nothing is building,
# and the backend engineer's _handle_test_build picks a spec from SPEC_POOL
# with get_next_spec() and queues it for the customer named in
# TEST_CUSTOMER_EMAIL through Actuators.queue_new_build. If that customer row
# does not exist, no build is queued.

import random

# Test customer for automated builds: uses e2e@craftandship.example
TEST_CUSTOMER_EMAIL = "e2e@craftandship.example"


def _generate_actions(spec: dict) -> list:
    """Auto-generate CRUD actions from data_models for a spec."""
    actions = []
    admin_role = spec["user_roles"][0]["role"]
    for model in spec.get("data_models", []):
        name_lower = model["name"].lower()
        actions.append({
            "name": f"create_{name_lower}",
            "role": admin_role,
            "trigger": "form_submit",
            "description": f"Create a new {model['name']}",
            "steps": [f"Validate {name_lower} fields", f"Save {name_lower} to database", f"Redirect to {name_lower} list"],
        })
        actions.append({
            "name": f"update_{name_lower}",
            "role": admin_role,
            "trigger": "form_submit",
            "description": f"Update an existing {model['name']}",
            "steps": [f"Validate {name_lower} fields", f"Update {name_lower} in database", f"Redirect to {name_lower} detail"],
        })
        actions.append({
            "name": f"delete_{name_lower}",
            "role": admin_role,
            "trigger": "button_click",
            "description": f"Delete a {model['name']}",
            "steps": [f"Confirm deletion", f"Delete {name_lower} from database", f"Redirect to {name_lower} list"],
        })
    return actions


_TYPE_MAP = {
    "string": "text", "date": "text", "number": "integer",
    "float": "integer", "url": "text", "phone": "text",
}

_VALID_STYLES = ["bold vibrant", "clean minimal", "corporate professional",
                 "modern dark", "modern light", "warm earthy"]
_STYLE_MAP = {
    "modern": "modern light", "clean": "clean minimal", "warm": "warm earthy",
    "professional": "corporate professional", "energetic": "bold vibrant",
    "friendly": "warm earthy", "industrial": "modern dark", "rustic": "warm earthy",
}


def _finalize_spec(spec: dict) -> dict:
    """Add required fields (actions, settings) and fix validation issues."""
    import copy
    spec = copy.deepcopy(spec)

    if "actions" not in spec:
        spec["actions"] = _generate_actions(spec)
    if "settings" not in spec:
        spec["settings"] = {"auth": True, "seed_data": True}

    # Add 'can' to roles if missing
    for role in spec.get("user_roles", []):
        if "can" not in role:
            role["can"] = "manage all" if role["role"] == "admin" else "view and edit own data"

    # Add 'allowed_roles' to pages if missing
    all_roles = [r["role"] for r in spec.get("user_roles", [])]
    for page in spec.get("pages", []):
        if "allowed_roles" not in page:
            page["allowed_roles"] = all_roles

    # Fix field types to valid set (boolean, email, integer, password, text, timestamp)
    for model in spec.get("data_models", []):
        for field in model.get("fields", []):
            if field["type"] in _TYPE_MAP:
                field["type"] = _TYPE_MAP[field["type"]]

    # Fix design.style to valid value
    design = spec.get("design", {})
    style = design.get("style", "")
    if style and style not in _VALID_STYLES:
        design["style"] = _STYLE_MAP.get(style, "modern light")

    return spec


# Pool of realistic Build-tier app specs (max 6 pages, 4 models, 3 roles)
SPEC_POOL = [
    {
        "app_name": "neighborhood-cms",
        "description": "A content management system for neighborhood associations to post announcements, manage events, and share community resources",
        "spec_version": "0.1",
        "admin_email": TEST_CUSTOMER_EMAIL,
        "user_roles": [
            {"role": "admin", "description": "Board members who manage content and events"},
            {"role": "resident", "description": "Neighborhood residents who view content and RSVP"},
        ],
        "pages": [
            {"title": "Dashboard", "path": "/dashboard", "purpose": "Overview of recent announcements and upcoming events", "auth_required": True},
            {"title": "Announcements", "path": "/announcements", "purpose": "List and create community announcements", "auth_required": True},
            {"title": "Events", "path": "/events", "purpose": "Community events calendar with RSVP", "auth_required": True},
            {"title": "Resources", "path": "/resources", "purpose": "Shared documents and links for residents", "auth_required": True},
        ],
        "data_models": [
            {"name": "Announcement", "fields": [
                {"name": "title", "type": "string", "required": True},
                {"name": "content", "type": "text", "required": True},
                {"name": "category", "type": "string", "required": True},
                {"name": "pinned", "type": "boolean", "required": False},
            ]},
            {"name": "Event", "fields": [
                {"name": "title", "type": "string", "required": True},
                {"name": "date", "type": "date", "required": True},
                {"name": "location", "type": "string", "required": True},
                {"name": "description", "type": "text", "required": False},
                {"name": "max_attendees", "type": "integer", "required": False},
            ]},
            {"name": "Resource", "fields": [
                {"name": "title", "type": "string", "required": True},
                {"name": "url", "type": "string", "required": True},
                {"name": "category", "type": "string", "required": True},
            ]},
        ],
        "design": {"primary_color": "#2563eb", "secondary_color": "#1e40af", "accent_color": "#f59e0b", "style": "modern"},
    },
    {
        "app_name": "recipe-meal-planner",
        "description": "A meal planning app where users save recipes, plan weekly meals, and generate shopping lists",
        "spec_version": "0.1",
        "admin_email": TEST_CUSTOMER_EMAIL,
        "user_roles": [
            {"role": "admin", "description": "App administrator"},
            {"role": "user", "description": "Regular users who plan meals"},
        ],
        "pages": [
            {"title": "Dashboard", "path": "/dashboard", "purpose": "Weekly meal plan overview", "auth_required": True},
            {"title": "Recipes", "path": "/recipes", "purpose": "Browse and manage recipe collection", "auth_required": True},
            {"title": "Meal Plan", "path": "/meal-plan", "purpose": "Drag-and-drop weekly meal planner", "auth_required": True},
            {"title": "Shopping List", "path": "/shopping-list", "purpose": "Auto-generated shopping list from meal plan", "auth_required": True},
        ],
        "data_models": [
            {"name": "Recipe", "fields": [
                {"name": "title", "type": "string", "required": True},
                {"name": "ingredients", "type": "text", "required": True},
                {"name": "instructions", "type": "text", "required": True},
                {"name": "prep_time_min", "type": "integer", "required": False},
                {"name": "servings", "type": "integer", "required": False},
                {"name": "category", "type": "string", "required": True},
            ]},
            {"name": "MealPlan", "fields": [
                {"name": "week_start", "type": "date", "required": True},
                {"name": "day_of_week", "type": "string", "required": True},
                {"name": "meal_type", "type": "string", "required": True},
            ], "belongs_to": "Recipe"},
            {"name": "ShoppingItem", "fields": [
                {"name": "ingredient", "type": "string", "required": True},
                {"name": "quantity", "type": "string", "required": False},
                {"name": "purchased", "type": "boolean", "required": False},
            ]},
        ],
        "design": {"primary_color": "#059669", "secondary_color": "#047857", "accent_color": "#f97316", "style": "warm"},
    },
    {
        "app_name": "fitness-class-booking",
        "description": "A booking system for a fitness studio where members reserve spots in classes and trainers manage schedules",
        "spec_version": "0.1",
        "admin_email": TEST_CUSTOMER_EMAIL,
        "user_roles": [
            {"role": "admin", "description": "Studio owner and trainers who manage classes"},
            {"role": "member", "description": "Gym members who book classes"},
        ],
        "pages": [
            {"title": "Schedule", "path": "/schedule", "purpose": "Weekly class schedule with booking", "auth_required": True},
            {"title": "My Bookings", "path": "/bookings", "purpose": "Member's upcoming and past bookings", "auth_required": True},
            {"title": "Classes", "path": "/classes", "purpose": "Manage class types and descriptions", "auth_required": True},
            {"title": "Members", "path": "/members", "purpose": "Admin view of all members and attendance", "auth_required": True},
        ],
        "data_models": [
            {"name": "FitnessClass", "fields": [
                {"name": "name", "type": "string", "required": True},
                {"name": "description", "type": "text", "required": False},
                {"name": "instructor", "type": "string", "required": True},
                {"name": "day_of_week", "type": "string", "required": True},
                {"name": "start_time", "type": "string", "required": True},
                {"name": "max_capacity", "type": "integer", "required": True},
            ]},
            {"name": "Booking", "fields": [
                {"name": "booking_date", "type": "date", "required": True},
                {"name": "status", "type": "string", "required": True},
                {"name": "notes", "type": "text", "required": False},
            ], "belongs_to": "FitnessClass"},
        ],
        "design": {"primary_color": "#7c3aed", "secondary_color": "#6d28d9", "accent_color": "#ec4899", "style": "energetic"},
    },
    {
        "app_name": "small-business-invoicer",
        "description": "Invoice management for freelancers and small businesses: create invoices, track payments, manage clients",
        "spec_version": "0.1",
        "admin_email": TEST_CUSTOMER_EMAIL,
        "user_roles": [
            {"role": "admin", "description": "Business owner who creates invoices"},
            {"role": "viewer", "description": "Accountant or bookkeeper with read access"},
        ],
        "pages": [
            {"title": "Dashboard", "path": "/dashboard", "purpose": "Revenue overview and outstanding invoices", "auth_required": True},
            {"title": "Invoices", "path": "/invoices", "purpose": "Create and manage invoices", "auth_required": True},
            {"title": "Clients", "path": "/clients", "purpose": "Client directory with contact info", "auth_required": True},
            {"title": "Reports", "path": "/reports", "purpose": "Monthly revenue and payment reports", "auth_required": True},
        ],
        "data_models": [
            {"name": "Client", "fields": [
                {"name": "company_name", "type": "string", "required": True},
                {"name": "contact_name", "type": "string", "required": True},
                {"name": "email", "type": "string", "required": True},
                {"name": "phone", "type": "string", "required": False},
                {"name": "address", "type": "text", "required": False},
            ]},
            {"name": "Invoice", "fields": [
                {"name": "invoice_number", "type": "string", "required": True},
                {"name": "amount", "type": "number", "required": True},
                {"name": "due_date", "type": "date", "required": True},
                {"name": "status", "type": "string", "required": True},
                {"name": "notes", "type": "text", "required": False},
            ], "belongs_to": "Client"},
            {"name": "LineItem", "fields": [
                {"name": "description", "type": "string", "required": True},
                {"name": "quantity", "type": "integer", "required": True},
                {"name": "unit_price", "type": "number", "required": True},
            ], "belongs_to": "Invoice"},
        ],
        "design": {"primary_color": "#0f766e", "secondary_color": "#115e59", "accent_color": "#eab308", "style": "professional"},
    },
    {
        "app_name": "volunteer-coordinator",
        "description": "Manage volunteer signups, track hours, and coordinate community service events for a nonprofit",
        "spec_version": "0.1",
        "admin_email": TEST_CUSTOMER_EMAIL,
        "user_roles": [
            {"role": "admin", "description": "Nonprofit coordinator who manages events"},
            {"role": "volunteer", "description": "Volunteers who sign up and log hours"},
        ],
        "pages": [
            {"title": "Dashboard", "path": "/dashboard", "purpose": "Upcoming events and total hours tracked", "auth_required": True},
            {"title": "Opportunities", "path": "/opportunities", "purpose": "Browse and sign up for volunteer events", "auth_required": True},
            {"title": "My Hours", "path": "/hours", "purpose": "Log and view personal volunteer hours", "auth_required": True},
            {"title": "Volunteers", "path": "/volunteers", "purpose": "Admin view of all volunteers and hours", "auth_required": True},
        ],
        "data_models": [
            {"name": "Opportunity", "fields": [
                {"name": "title", "type": "string", "required": True},
                {"name": "date", "type": "date", "required": True},
                {"name": "location", "type": "string", "required": True},
                {"name": "description", "type": "text", "required": False},
                {"name": "spots_available", "type": "integer", "required": True},
            ]},
            {"name": "HourLog", "fields": [
                {"name": "date", "type": "date", "required": True},
                {"name": "hours", "type": "number", "required": True},
                {"name": "description", "type": "string", "required": True},
                {"name": "verified", "type": "boolean", "required": False},
            ], "belongs_to": "Opportunity"},
        ],
        "design": {"primary_color": "#dc2626", "secondary_color": "#b91c1c", "accent_color": "#2563eb", "style": "friendly"},
    },
    {
        "app_name": "equipment-rental-tracker",
        "description": "Track equipment rentals, availability, and maintenance for a rental business",
        "spec_version": "0.1",
        "admin_email": TEST_CUSTOMER_EMAIL,
        "user_roles": [
            {"role": "admin", "description": "Rental business owner"},
            {"role": "staff", "description": "Staff who process rentals"},
        ],
        "pages": [
            {"title": "Dashboard", "path": "/dashboard", "purpose": "Active rentals and equipment status", "auth_required": True},
            {"title": "Equipment", "path": "/equipment", "purpose": "Inventory of all rental equipment", "auth_required": True},
            {"title": "Rentals", "path": "/rentals", "purpose": "Active and past rental records", "auth_required": True},
            {"title": "Maintenance", "path": "/maintenance", "purpose": "Maintenance schedule and history", "auth_required": True},
        ],
        "data_models": [
            {"name": "Equipment", "fields": [
                {"name": "name", "type": "string", "required": True},
                {"name": "category", "type": "string", "required": True},
                {"name": "daily_rate", "type": "number", "required": True},
                {"name": "status", "type": "string", "required": True},
                {"name": "serial_number", "type": "string", "required": False},
            ]},
            {"name": "Rental", "fields": [
                {"name": "customer_name", "type": "string", "required": True},
                {"name": "start_date", "type": "date", "required": True},
                {"name": "end_date", "type": "date", "required": True},
                {"name": "total_cost", "type": "number", "required": False},
                {"name": "status", "type": "string", "required": True},
            ], "belongs_to": "Equipment"},
            {"name": "MaintenanceLog", "fields": [
                {"name": "date", "type": "date", "required": True},
                {"name": "description", "type": "text", "required": True},
                {"name": "cost", "type": "number", "required": False},
            ], "belongs_to": "Equipment"},
        ],
        "design": {"primary_color": "#d97706", "secondary_color": "#b45309", "accent_color": "#0284c7", "style": "industrial"},
    },
    {
        "app_name": "study-group-hub",
        "description": "A platform for students to form study groups, share notes, and schedule study sessions",
        "spec_version": "0.1",
        "admin_email": TEST_CUSTOMER_EMAIL,
        "user_roles": [
            {"role": "admin", "description": "Course administrator"},
            {"role": "student", "description": "Students in the course"},
        ],
        "pages": [
            {"title": "Dashboard", "path": "/dashboard", "purpose": "My groups and upcoming sessions", "auth_required": True},
            {"title": "Groups", "path": "/groups", "purpose": "Browse and join study groups", "auth_required": True},
            {"title": "Sessions", "path": "/sessions", "purpose": "Schedule and view study sessions", "auth_required": True},
            {"title": "Notes", "path": "/notes", "purpose": "Shared notes and resources", "auth_required": True},
        ],
        "data_models": [
            {"name": "StudyGroup", "fields": [
                {"name": "name", "type": "string", "required": True},
                {"name": "subject", "type": "string", "required": True},
                {"name": "description", "type": "text", "required": False},
                {"name": "max_members", "type": "integer", "required": False},
            ]},
            {"name": "Session", "fields": [
                {"name": "date", "type": "date", "required": True},
                {"name": "time", "type": "string", "required": True},
                {"name": "location", "type": "string", "required": True},
                {"name": "topic", "type": "string", "required": True},
            ], "belongs_to": "StudyGroup"},
            {"name": "Note", "fields": [
                {"name": "title", "type": "string", "required": True},
                {"name": "content", "type": "text", "required": True},
                {"name": "subject", "type": "string", "required": True},
            ], "belongs_to": "StudyGroup"},
        ],
        "design": {"primary_color": "#4f46e5", "secondary_color": "#4338ca", "accent_color": "#10b981", "style": "clean"},
    },
    {
        "app_name": "local-farm-market",
        "description": "An ordering system for a local farm stand: customers browse products, place orders for pickup",
        "spec_version": "0.1",
        "admin_email": TEST_CUSTOMER_EMAIL,
        "user_roles": [
            {"role": "admin", "description": "Farm owner managing inventory and orders"},
            {"role": "customer", "description": "Customers browsing and ordering produce"},
        ],
        "pages": [
            {"title": "Shop", "path": "/shop", "purpose": "Browse available produce and products", "auth_required": True},
            {"title": "Orders", "path": "/orders", "purpose": "View and manage orders", "auth_required": True},
            {"title": "Products", "path": "/products", "purpose": "Admin product inventory management", "auth_required": True},
            {"title": "Dashboard", "path": "/dashboard", "purpose": "Sales overview and order stats", "auth_required": True},
        ],
        "data_models": [
            {"name": "Product", "fields": [
                {"name": "name", "type": "string", "required": True},
                {"name": "price", "type": "number", "required": True},
                {"name": "unit", "type": "string", "required": True},
                {"name": "in_stock", "type": "boolean", "required": True},
                {"name": "category", "type": "string", "required": True},
            ]},
            {"name": "Order", "fields": [
                {"name": "pickup_date", "type": "date", "required": True},
                {"name": "status", "type": "string", "required": True},
                {"name": "total", "type": "number", "required": False},
                {"name": "notes", "type": "text", "required": False},
            ]},
            {"name": "OrderItem", "fields": [
                {"name": "quantity", "type": "integer", "required": True},
                {"name": "unit_price", "type": "number", "required": True},
            ], "belongs_to": "Product,Order"},
        ],
        "design": {"primary_color": "#16a34a", "secondary_color": "#15803d", "accent_color": "#ca8a04", "style": "rustic"},
    },
]


def get_next_spec(conn) -> dict | None:
    """Pick a spec that hasn't been built recently. Returns None if all have active builds."""
    cur = conn.cursor()
    cur.execute(
        "SELECT app_name FROM builds WHERE status IN ('queued', 'building', 'testing', 'deploying') "
        "UNION SELECT app_name FROM builds WHERE status IN ('live', 'preview') "
        "AND created_at > NOW() - INTERVAL '7 days'"
    )
    active_names = {row[0] for row in cur.fetchall()}
    cur.close()

    available = [s for s in SPEC_POOL if s["app_name"] not in active_names]
    if not available:
        return None
    return _finalize_spec(random.choice(available))


def get_test_customer_id(conn) -> str | None:
    """Look up the test customer for automated builds (never creates it); None if absent."""
    cur = conn.cursor()
    cur.execute("SELECT id FROM customers WHERE email = %s", (TEST_CUSTOMER_EMAIL,))
    row = cur.fetchone()
    cur.close()
    if row:
        return str(row[0])
    return None
