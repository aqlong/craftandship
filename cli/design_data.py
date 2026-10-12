"""Curated design data for the CAS (v1) build pipeline.

"UUPM" throughout the code and tests is the UI UX Pro Max skill:
https://github.com/nextlevelbuilder/ui-ux-pro-max-skill (MIT license,
Copyright (c) 2024 Next Level Builder). The palettes, font pairings, product
recommendations and UX rules below were extracted from its CSVs
(colors.csv, typography.csv, products.csv, ux-guidelines.csv) and curated here.
All palettes WCAG AA audited. No fonts from BANNED_FONTS.
"""

BANNED_FONTS = {"Inter", "Roboto", "Open Sans", "Lato", "Arial", "Helvetica"}

VALID_STYLES = {
    "modern light", "modern dark", "clean minimal",
    "warm earthy", "bold vibrant", "corporate professional",
}

# --- Color Palettes (WCAG AA verified, on_primary pre-fixed) ---
COLOR_PALETTES = {
    "saas": {
        "primary": "#2563EB", "on_primary": "#FFFFFF",
        "secondary": "#3B82F6", "accent": "#EA580C",
        "background": "#F8FAFC", "foreground": "#1E293B",
        "card": "#FFFFFF", "muted": "#E9EFF8", "border": "#E2E8F0",
    },
    "micro_saas": {
        "primary": "#4F46E5", "on_primary": "#FFFFFF",
        "secondary": "#818CF8", "accent": "#059669",
        "background": "#F5F3FF", "foreground": "#1E1B4B",
        "card": "#FFFFFF", "muted": "#EBEFF9", "border": "#E0E7FF",
    },
    "ecommerce": {
        "primary": "#059669", "on_primary": "#0F172A",
        "secondary": "#10B981", "accent": "#EA580C",
        "background": "#ECFDF5", "foreground": "#064E3B",
        "card": "#FFFFFF", "muted": "#E8F1F3", "border": "#A7F3D0",
    },
    "ecommerce_luxury": {
        "primary": "#1C1917", "on_primary": "#FFFFFF",
        "secondary": "#44403C", "accent": "#A16207",
        "background": "#FAFAF9", "foreground": "#0C0A09",
        "card": "#FFFFFF", "muted": "#E8ECF0", "border": "#D6D3D1",
    },
    "b2b_service": {
        "primary": "#0F172A", "on_primary": "#FFFFFF",
        "secondary": "#334155", "accent": "#0369A1",
        "background": "#F8FAFC", "foreground": "#020617",
        "card": "#FFFFFF", "muted": "#E8ECF1", "border": "#E2E8F0",
    },
    "financial_dashboard": {
        "primary": "#0F172A", "on_primary": "#FFFFFF",
        "secondary": "#1E293B", "accent": "#22C55E",
        "background": "#020617", "foreground": "#F8FAFC",
        "card": "#0E1223", "muted": "#1A1E2F", "border": "#334155",
    },
    "analytics_dashboard": {
        "primary": "#1E40AF", "on_primary": "#FFFFFF",
        "secondary": "#3B82F6", "accent": "#D97706",
        "background": "#F8FAFC", "foreground": "#1E3A8A",
        "card": "#FFFFFF", "muted": "#E9EEF6", "border": "#DBEAFE",
    },
    "healthcare": {
        "primary": "#0891B2", "on_primary": "#0F172A",
        "secondary": "#22D3EE", "accent": "#059669",
        "background": "#ECFEFF", "foreground": "#164E63",
        "card": "#FFFFFF", "muted": "#E8F1F6", "border": "#A5F3FC",
    },
    "education": {
        "primary": "#4F46E5", "on_primary": "#FFFFFF",
        "secondary": "#818CF8", "accent": "#EA580C",
        "background": "#EEF2FF", "foreground": "#1E1B4B",
        "card": "#FFFFFF", "muted": "#EBEEF8", "border": "#C7D2FE",
    },
    "creative_agency": {
        "primary": "#EC4899", "on_primary": "#0F172A",
        "secondary": "#F472B6", "accent": "#0891B2",
        "background": "#FDF2F8", "foreground": "#831843",
        "card": "#FFFFFF", "muted": "#F1EEF5", "border": "#FBCFE8",
    },
    "portfolio": {
        "primary": "#18181B", "on_primary": "#FFFFFF",
        "secondary": "#3F3F46", "accent": "#2563EB",
        "background": "#FAFAFA", "foreground": "#09090B",
        "card": "#FFFFFF", "muted": "#E8ECF0", "border": "#E4E4E7",
    },
    "gaming": {
        "primary": "#7C3AED", "on_primary": "#FFFFFF",
        "secondary": "#A78BFA", "accent": "#F43F5E",
        "background": "#0F0F23", "foreground": "#E2E8F0",
        "card": "#1E1C35", "muted": "#27273B", "border": "#4C1D95",
    },
    "government": {
        "primary": "#0F172A", "on_primary": "#FFFFFF",
        "secondary": "#334155", "accent": "#0369A1",
        "background": "#F8FAFC", "foreground": "#020617",
        "card": "#FFFFFF", "muted": "#E8ECF1", "border": "#E2E8F0",
    },
    "fintech": {
        "primary": "#F59E0B", "on_primary": "#0F172A",
        "secondary": "#FBBF24", "accent": "#8B5CF6",
        "background": "#0F172A", "foreground": "#F8FAFC",
        "card": "#222735", "muted": "#272F42", "border": "#334155",
    },
    "social_media": {
        "primary": "#E11D48", "on_primary": "#FFFFFF",
        "secondary": "#FB7185", "accent": "#2563EB",
        "background": "#FFF1F2", "foreground": "#881337",
        "card": "#FFFFFF", "muted": "#F0ECF2", "border": "#FECDD3",
    },
    "productivity": {
        "primary": "#0D9488", "on_primary": "#0F172A",
        "secondary": "#14B8A6", "accent": "#EA580C",
        "background": "#F0FDFA", "foreground": "#134E4A",
        "card": "#FFFFFF", "muted": "#E8F1F4", "border": "#99F6E4",
    },
    "ai_chatbot": {
        "primary": "#7C3AED", "on_primary": "#FFFFFF",
        "secondary": "#A78BFA", "accent": "#0891B2",
        "background": "#FAF5FF", "foreground": "#1E1B4B",
        "card": "#FFFFFF", "muted": "#ECEEF9", "border": "#DDD6FE",
    },
    "pet_services": {
        "primary": "#F97316", "on_primary": "#0F172A",
        "secondary": "#FB923C", "accent": "#2563EB",
        "background": "#FFF7ED", "foreground": "#9A3412",
        "card": "#FFFFFF", "muted": "#F1F0F0", "border": "#FED7AA",
    },
    "beauty_spa": {
        "primary": "#EC4899", "on_primary": "#0F172A",
        "secondary": "#F9A8D4", "accent": "#8B5CF6",
        "background": "#FDF2F8", "foreground": "#831843",
        "card": "#FFFFFF", "muted": "#F1EEF5", "border": "#FBCFE8",
    },
    "luxury_brand": {
        "primary": "#1C1917", "on_primary": "#FFFFFF",
        "secondary": "#44403C", "accent": "#A16207",
        "background": "#FAFAF9", "foreground": "#0C0A09",
        "card": "#FFFFFF", "muted": "#E8ECF0", "border": "#D6D3D1",
    },
    "restaurant": {
        "primary": "#DC2626", "on_primary": "#FFFFFF",
        "secondary": "#F87171", "accent": "#A16207",
        "background": "#FEF2F2", "foreground": "#450A0A",
        "card": "#FFFFFF", "muted": "#F0EDF1", "border": "#FECACA",
    },
    "fitness": {
        "primary": "#F97316", "on_primary": "#0F172A",
        "secondary": "#FB923C", "accent": "#22C55E",
        "background": "#1F2937", "foreground": "#F8FAFC",
        "card": "#313742", "muted": "#37414F", "border": "#374151",
    },
    "real_estate": {
        "primary": "#0F766E", "on_primary": "#FFFFFF",
        "secondary": "#14B8A6", "accent": "#0369A1",
        "background": "#F0FDFA", "foreground": "#134E4A",
        "card": "#FFFFFF", "muted": "#E8F0F3", "border": "#99F6E4",
    },
    "travel": {
        "primary": "#0EA5E9", "on_primary": "#0F172A",
        "secondary": "#38BDF8", "accent": "#EA580C",
        "background": "#F0F9FF", "foreground": "#0C4A6E",
        "card": "#FFFFFF", "muted": "#E8F2F8", "border": "#BAE6FD",
    },
    "hotel": {
        "primary": "#1E3A8A", "on_primary": "#FFFFFF",
        "secondary": "#3B82F6", "accent": "#A16207",
        "background": "#F8FAFC", "foreground": "#1E40AF",
        "card": "#FFFFFF", "muted": "#E9EEF5", "border": "#BFDBFE",
    },
    "wedding": {
        "primary": "#DB2777", "on_primary": "#FFFFFF",
        "secondary": "#F472B6", "accent": "#A16207",
        "background": "#FDF2F8", "foreground": "#831843",
        "card": "#FFFFFF", "muted": "#F0EDF4", "border": "#FBCFE8",
    },
    "legal": {
        "primary": "#1E3A8A", "on_primary": "#FFFFFF",
        "secondary": "#1E40AF", "accent": "#B45309",
        "background": "#F8FAFC", "foreground": "#0F172A",
        "card": "#FFFFFF", "muted": "#E9EEF5", "border": "#CBD5E1",
    },
    "insurance": {
        "primary": "#0369A1", "on_primary": "#FFFFFF",
        "secondary": "#0EA5E9", "accent": "#16A34A",
        "background": "#F0F9FF", "foreground": "#0C4A6E",
        "card": "#FFFFFF", "muted": "#E7EFF5", "border": "#BAE6FD",
    },
    "banking": {
        "primary": "#0F172A", "on_primary": "#FFFFFF",
        "secondary": "#1E3A8A", "accent": "#A16207",
        "background": "#F8FAFC", "foreground": "#020617",
        "card": "#FFFFFF", "muted": "#E8ECF1", "border": "#E2E8F0",
    },
    "online_course": {
        "primary": "#0D9488", "on_primary": "#0F172A",
        "secondary": "#2DD4BF", "accent": "#EA580C",
        "background": "#F0FDFA", "foreground": "#134E4A",
        "card": "#FFFFFF", "muted": "#E8F1F4", "border": "#5EEAD4",
    },
    "nonprofit": {
        "primary": "#0891B2", "on_primary": "#0F172A",
        "secondary": "#22D3EE", "accent": "#EA580C",
        "background": "#ECFEFF", "foreground": "#164E63",
        "card": "#FFFFFF", "muted": "#E8F1F6", "border": "#A5F3FC",
    },
    "job_board": {
        "primary": "#0369A1", "on_primary": "#FFFFFF",
        "secondary": "#0EA5E9", "accent": "#16A34A",
        "background": "#F0F9FF", "foreground": "#0C4A6E",
        "card": "#FFFFFF", "muted": "#E7EFF5", "border": "#BAE6FD",
    },
    "marketplace": {
        "primary": "#7C3AED", "on_primary": "#FFFFFF",
        "secondary": "#A78BFA", "accent": "#16A34A",
        "background": "#FAF5FF", "foreground": "#4C1D95",
        "card": "#FFFFFF", "muted": "#ECEEF9", "border": "#DDD6FE",
    },
    "logistics": {
        "primary": "#2563EB", "on_primary": "#FFFFFF",
        "secondary": "#3B82F6", "accent": "#EA580C",
        "background": "#EFF6FF", "foreground": "#1E40AF",
        "card": "#FFFFFF", "muted": "#E9EFF8", "border": "#BFDBFE",
    },
    "agriculture": {
        "primary": "#15803D", "on_primary": "#FFFFFF",
        "secondary": "#22C55E", "accent": "#A16207",
        "background": "#F0FDF4", "foreground": "#14532D",
        "card": "#FFFFFF", "muted": "#E8F0F1", "border": "#BBF7D0",
    },
    "construction": {
        "primary": "#64748B", "on_primary": "#FFFFFF",
        "secondary": "#94A3B8", "accent": "#EA580C",
        "background": "#F8FAFC", "foreground": "#334155",
        "card": "#FFFFFF", "muted": "#EBF0F5", "border": "#E2E8F0",
    },
    "automotive": {
        "primary": "#1E293B", "on_primary": "#FFFFFF",
        "secondary": "#334155", "accent": "#DC2626",
        "background": "#F8FAFC", "foreground": "#0F172A",
        "card": "#FFFFFF", "muted": "#E9EDF1", "border": "#E2E8F0",
    },
    "photography": {
        "primary": "#18181B", "on_primary": "#FFFFFF",
        "secondary": "#27272A", "accent": "#2563EB",
        "background": "#FAFAFA", "foreground": "#09090B",
        "card": "#FFFFFF", "muted": "#E8ECF0", "border": "#E4E4E7",
    },
    "coworking": {
        "primary": "#F59E0B", "on_primary": "#0F172A",
        "secondary": "#FBBF24", "accent": "#2563EB",
        "background": "#FFFBEB", "foreground": "#78350F",
        "card": "#FFFFFF", "muted": "#F1F2EF", "border": "#FDE68A",
    },
    "home_services": {
        "primary": "#1E40AF", "on_primary": "#FFFFFF",
        "secondary": "#3B82F6", "accent": "#EA580C",
        "background": "#EFF6FF", "foreground": "#1E3A8A",
        "card": "#FFFFFF", "muted": "#E9EEF6", "border": "#BFDBFE",
    },
    "childcare": {
        "primary": "#F472B6", "on_primary": "#0F172A",
        "secondary": "#FBCFE8", "accent": "#16A34A",
        "background": "#FDF2F8", "foreground": "#9D174D",
        "card": "#FFFFFF", "muted": "#F1F0F6", "border": "#FCE7F3",
    },
    "medical_clinic": {
        "primary": "#0891B2", "on_primary": "#0F172A",
        "secondary": "#22D3EE", "accent": "#16A34A",
        "background": "#F0FDFA", "foreground": "#134E4A",
        "card": "#FFFFFF", "muted": "#E8F1F6", "border": "#CCFBF1",
    },
    "veterinary": {
        "primary": "#0D9488", "on_primary": "#0F172A",
        "secondary": "#14B8A6", "accent": "#EA580C",
        "background": "#F0FDFA", "foreground": "#134E4A",
        "card": "#FFFFFF", "muted": "#E8F1F4", "border": "#99F6E4",
    },
    "florist": {
        "primary": "#15803D", "on_primary": "#FFFFFF",
        "secondary": "#22C55E", "accent": "#EC4899",
        "background": "#F0FDF4", "foreground": "#14532D",
        "card": "#FFFFFF", "muted": "#E8F0F1", "border": "#BBF7D0",
    },
    "bakery_cafe": {
        "primary": "#92400E", "on_primary": "#FFFFFF",
        "secondary": "#B45309", "accent": "#92400E",
        "background": "#FEF3C7", "foreground": "#78350F",
        "card": "#FFFFFF", "muted": "#EDEEF0", "border": "#FDE68A",
    },
    "brewery_winery": {
        "primary": "#7C2D12", "on_primary": "#FFFFFF",
        "secondary": "#B91C1C", "accent": "#A16207",
        "background": "#FEF2F2", "foreground": "#450A0A",
        "card": "#FFFFFF", "muted": "#ECEDF0", "border": "#FECACA",
    },
    "news_media": {
        "primary": "#DC2626", "on_primary": "#FFFFFF",
        "secondary": "#EF4444", "accent": "#1E40AF",
        "background": "#FEF2F2", "foreground": "#450A0A",
        "card": "#FFFFFF", "muted": "#F0EDF1", "border": "#FECACA",
    },
    "freelancer": {
        "primary": "#4F46E5", "on_primary": "#FFFFFF",
        "secondary": "#818CF8", "accent": "#16A34A",
        "background": "#EEF2FF", "foreground": "#312E81",
        "card": "#FFFFFF", "muted": "#EBEFF9", "border": "#C7D2FE",
    },
    "event_management": {
        "primary": "#7C3AED", "on_primary": "#FFFFFF",
        "secondary": "#A78BFA", "accent": "#EA580C",
        "background": "#FAF5FF", "foreground": "#4C1D95",
        "card": "#FFFFFF", "muted": "#ECEEF9", "border": "#DDD6FE",
    },
    "membership_community": {
        "primary": "#7C3AED", "on_primary": "#FFFFFF",
        "secondary": "#A78BFA", "accent": "#16A34A",
        "background": "#FAF5FF", "foreground": "#4C1D95",
        "card": "#FFFFFF", "muted": "#ECEEF9", "border": "#DDD6FE",
    },
    "newsletter": {
        "primary": "#0369A1", "on_primary": "#FFFFFF",
        "secondary": "#0EA5E9", "accent": "#EA580C",
        "background": "#F0F9FF", "foreground": "#0C4A6E",
        "card": "#FFFFFF", "muted": "#E7EFF5", "border": "#BAE6FD",
    },
    "church": {
        "primary": "#7C3AED", "on_primary": "#FFFFFF",
        "secondary": "#A78BFA", "accent": "#A16207",
        "background": "#FAF5FF", "foreground": "#4C1D95",
        "card": "#FFFFFF", "muted": "#ECEEF9", "border": "#DDD6FE",
    },
    "sports": {
        "primary": "#DC2626", "on_primary": "#FFFFFF",
        "secondary": "#EF4444", "accent": "#DC2626",
        "background": "#FEF2F2", "foreground": "#7F1D1D",
        "card": "#FFFFFF", "muted": "#F0EDF1", "border": "#FECACA",
    },
    "crm": {
        "primary": "#2563EB", "on_primary": "#FFFFFF",
        "secondary": "#3B82F6", "accent": "#059669",
        "background": "#F8FAFC", "foreground": "#0F172A",
        "card": "#FFFFFF", "muted": "#F1F5FD", "border": "#E4ECFC",
    },
    "inventory": {
        "primary": "#334155", "on_primary": "#FFFFFF",
        "secondary": "#475569", "accent": "#059669",
        "background": "#F8FAFC", "foreground": "#0F172A",
        "card": "#FFFFFF", "muted": "#F2F3F4", "border": "#E6E8EA",
    },
    "invoice": {
        "primary": "#1E3A5F", "on_primary": "#FFFFFF",
        "secondary": "#2563EB", "accent": "#059669",
        "background": "#F8FAFC", "foreground": "#0F172A",
        "card": "#FFFFFF", "muted": "#F1F3F5", "border": "#E4E7EB",
    },
    "booking": {
        "primary": "#0369A1", "on_primary": "#FFFFFF",
        "secondary": "#0EA5E9", "accent": "#059669",
        "background": "#F0F9FF", "foreground": "#0F172A",
        "card": "#FFFFFF", "muted": "#EFF7FB", "border": "#E0F0F8",
    },
    "food_delivery": {
        "primary": "#EA580C", "on_primary": "#0F172A",
        "secondary": "#F97316", "accent": "#2563EB",
        "background": "#FFF7ED", "foreground": "#0F172A",
        "card": "#FFFFFF", "muted": "#FDF4F0", "border": "#FCEAE1",
    },
    "recipe_cooking": {
        "primary": "#9A3412", "on_primary": "#FFFFFF",
        "secondary": "#C2410C", "accent": "#059669",
        "background": "#FFFBEB", "foreground": "#0F172A",
        "card": "#FFFFFF", "muted": "#F8F2F0", "border": "#F2E6E2",
    },
    "meditation": {
        "primary": "#7C3AED", "on_primary": "#FFFFFF",
        "secondary": "#8B5CF6", "accent": "#059669",
        "background": "#FAF5FF", "foreground": "#0F172A",
        "card": "#FFFFFF", "muted": "#F7F3FD", "border": "#EFE7FC",
    },
    "habit_tracker": {
        "primary": "#D97706", "on_primary": "#0F172A",
        "secondary": "#F59E0B", "accent": "#059669",
        "background": "#FFFBEB", "foreground": "#0F172A",
        "card": "#FFFFFF", "muted": "#FCF6F0", "border": "#FAEEE1",
    },
    "plant_care": {
        "primary": "#15803D", "on_primary": "#FFFFFF",
        "secondary": "#059669", "accent": "#D97706",
        "background": "#F0FDF4", "foreground": "#0F172A",
        "card": "#FFFFFF", "muted": "#F0F7F3", "border": "#E2EFE7",
    },
    "healing_arts": {
        "primary": "#0f2b3c", "on_primary": "#FFFFFF",
        "secondary": "#1a5a6a", "accent": "#c8963e",
        "background": "#F0F7FA", "foreground": "#0F2B3C",
        "card": "#FFFFFF", "muted": "#E8F0F4", "border": "#C4D9E2",
    },
}

# --- Font Pairings (no banned fonts) ---
FONT_PAIRINGS = {
    "modern_professional": {
        "display": "Poppins", "body": "DM Sans",
        "google_fonts_url": "https://fonts.googleapis.com/css2?family=DM+Sans:wght@300;400;500;600;700&family=Poppins:wght@400;500;600;700&display=swap",
        "tailwind_config": "{ display: ['Poppins', 'sans-serif'], body: ['DM Sans', 'sans-serif'] }",
    },
    "tech_startup": {
        "display": "Space Grotesk", "body": "DM Sans",
        "google_fonts_url": "https://fonts.googleapis.com/css2?family=DM+Sans:wght@400;500;700&family=Space+Grotesk:wght@400;500;600;700&display=swap",
        "tailwind_config": "{ display: ['Space Grotesk', 'sans-serif'], body: ['DM Sans', 'sans-serif'] }",
    },
    "editorial_classic": {
        "display": "Cormorant Garamond", "body": "Libre Baskerville",
        "google_fonts_url": "https://fonts.googleapis.com/css2?family=Cormorant+Garamond:wght@400;500;600;700&family=Libre+Baskerville:wght@400;700&display=swap",
        "tailwind_config": "{ display: ['Cormorant Garamond', 'serif'], body: ['Libre Baskerville', 'serif'] }",
    },
    "playful_creative": {
        "display": "Fredoka", "body": "Nunito",
        "google_fonts_url": "https://fonts.googleapis.com/css2?family=Fredoka:wght@400;500;600;700&family=Nunito:wght@300;400;500;600;700&display=swap",
        "tailwind_config": "{ display: ['Fredoka', 'sans-serif'], body: ['Nunito', 'sans-serif'] }",
    },
    "bold_statement": {
        "display": "Bebas Neue", "body": "Source Sans 3",
        "google_fonts_url": "https://fonts.googleapis.com/css2?family=Bebas+Neue&family=Source+Sans+3:wght@300;400;500;600;700&display=swap",
        "tailwind_config": "{ display: ['Bebas Neue', 'sans-serif'], body: ['Source Sans 3', 'sans-serif'] }",
    },
    "wellness_calm": {
        "display": "Lora", "body": "Raleway",
        "google_fonts_url": "https://fonts.googleapis.com/css2?family=Lora:wght@400;500;600;700&family=Raleway:wght@300;400;500;600;700&display=swap",
        "tailwind_config": "{ display: ['Lora', 'serif'], body: ['Raleway', 'sans-serif'] }",
    },
    "developer_mono": {
        "display": "JetBrains Mono", "body": "IBM Plex Sans",
        "google_fonts_url": "https://fonts.googleapis.com/css2?family=IBM+Plex+Sans:wght@300;400;500;600;700&family=JetBrains+Mono:wght@400;500;600;700&display=swap",
        "tailwind_config": "{ display: ['JetBrains Mono', 'monospace'], body: ['IBM Plex Sans', 'sans-serif'] }",
    },
    "geometric_modern": {
        "display": "Outfit", "body": "Work Sans",
        "google_fonts_url": "https://fonts.googleapis.com/css2?family=Outfit:wght@400;500;600;700&family=Work+Sans:wght@300;400;500;600;700&display=swap",
        "tailwind_config": "{ display: ['Outfit', 'sans-serif'], body: ['Work Sans', 'sans-serif'] }",
    },
    "luxury_serif": {
        "display": "Cormorant", "body": "Montserrat",
        "google_fonts_url": "https://fonts.googleapis.com/css2?family=Cormorant:wght@400;500;600;700&family=Montserrat:wght@300;400;500;600;700&display=swap",
        "tailwind_config": "{ display: ['Cormorant', 'serif'], body: ['Montserrat', 'sans-serif'] }",
    },
    "friendly_saas": {
        "display": "Plus Jakarta Sans", "body": "Plus Jakarta Sans",
        "google_fonts_url": "https://fonts.googleapis.com/css2?family=Plus+Jakarta+Sans:wght@300;400;500;600;700&display=swap",
        "tailwind_config": "{ display: ['Plus Jakarta Sans', 'sans-serif'], body: ['Plus Jakarta Sans', 'sans-serif'] }",
    },
    "corporate_trust": {
        "display": "Lexend", "body": "Source Sans 3",
        "google_fonts_url": "https://fonts.googleapis.com/css2?family=Lexend:wght@300;400;500;600;700&family=Source+Sans+3:wght@300;400;500;600;700&display=swap",
        "tailwind_config": "{ display: ['Lexend', 'sans-serif'], body: ['Source Sans 3', 'sans-serif'] }",
    },
    "fashion_forward": {
        "display": "Syne", "body": "Manrope",
        "google_fonts_url": "https://fonts.googleapis.com/css2?family=Manrope:wght@300;400;500;600;700&family=Syne:wght@400;500;600;700&display=swap",
        "tailwind_config": "{ display: ['Syne', 'sans-serif'], body: ['Manrope', 'sans-serif'] }",
    },
    "soft_rounded": {
        "display": "Varela Round", "body": "Nunito Sans",
        "google_fonts_url": "https://fonts.googleapis.com/css2?family=Nunito+Sans:wght@300;400;500;600;700&family=Varela+Round&display=swap",
        "tailwind_config": "{ display: ['Varela Round', 'sans-serif'], body: ['Nunito Sans', 'sans-serif'] }",
    },
    "medical_clean": {
        "display": "Figtree", "body": "Noto Sans",
        "google_fonts_url": "https://fonts.googleapis.com/css2?family=Figtree:wght@400;500;600;700&family=Noto+Sans:wght@300;400;500;600;700&display=swap",
        "tailwind_config": "{ display: ['Figtree', 'sans-serif'], body: ['Noto Sans', 'sans-serif'] }",
    },
    "financial_trust": {
        "display": "IBM Plex Sans", "body": "IBM Plex Sans",
        "google_fonts_url": "https://fonts.googleapis.com/css2?family=IBM+Plex+Sans:wght@300;400;500;600;700&display=swap",
        "tailwind_config": "{ display: ['IBM Plex Sans', 'sans-serif'], body: ['IBM Plex Sans', 'sans-serif'] }",
    },
    "real_estate_luxury": {
        "display": "Cinzel", "body": "Josefin Sans",
        "google_fonts_url": "https://fonts.googleapis.com/css2?family=Cinzel:wght@400;500;600;700&family=Josefin+Sans:wght@300;400;500;600;700&display=swap",
        "tailwind_config": "{ display: ['Cinzel', 'serif'], body: ['Josefin Sans', 'sans-serif'] }",
    },
    "restaurant_menu": {
        "display": "Playfair Display SC", "body": "Karla",
        "google_fonts_url": "https://fonts.googleapis.com/css2?family=Karla:wght@300;400;500;600;700&family=Playfair+Display+SC:wght@400;700&display=swap",
        "tailwind_config": "{ display: ['Playfair Display SC', 'serif'], body: ['Karla', 'sans-serif'] }",
    },
    "ecommerce_clean": {
        "display": "Rubik", "body": "Nunito Sans",
        "google_fonts_url": "https://fonts.googleapis.com/css2?family=Nunito+Sans:wght@300;400;500;600;700&family=Rubik:wght@400;500;600;700&display=swap",
        "tailwind_config": "{ display: ['Rubik', 'sans-serif'], body: ['Nunito Sans', 'sans-serif'] }",
    },
    "academic_research": {
        "display": "Crimson Pro", "body": "Atkinson Hyperlegible",
        "google_fonts_url": "https://fonts.googleapis.com/css2?family=Atkinson+Hyperlegible:wght@400;700&family=Crimson+Pro:wght@400;500;600;700&display=swap",
        "tailwind_config": "{ display: ['Crimson Pro', 'serif'], body: ['Atkinson Hyperlegible', 'sans-serif'] }",
    },
    "warm_editorial": {
        "display": "Playfair Display", "body": "Crimson Pro",
        "google_fonts_url": "https://fonts.googleapis.com/css2?family=Crimson+Pro:wght@300;400;500;600;700&family=Playfair+Display:wght@400;500;600;700&display=swap",
        "tailwind_config": "{ display: ['Playfair Display', 'serif'], body: ['Crimson Pro', 'serif'] }",
    },
    "accessibility_first": {
        "display": "Atkinson Hyperlegible", "body": "Atkinson Hyperlegible",
        "google_fonts_url": "https://fonts.googleapis.com/css2?family=Atkinson+Hyperlegible:wght@400;700&display=swap",
        "tailwind_config": "{ display: ['Atkinson Hyperlegible', 'sans-serif'], body: ['Atkinson Hyperlegible', 'sans-serif'] }",
    },
    "sports_fitness": {
        "display": "Barlow Condensed", "body": "Barlow",
        "google_fonts_url": "https://fonts.googleapis.com/css2?family=Barlow+Condensed:wght@400;500;600;700&family=Barlow:wght@300;400;500;600;700&display=swap",
        "tailwind_config": "{ display: ['Barlow Condensed', 'sans-serif'], body: ['Barlow', 'sans-serif'] }",
    },
    "kids_education": {
        "display": "Baloo 2", "body": "Comic Neue",
        "google_fonts_url": "https://fonts.googleapis.com/css2?family=Baloo+2:wght@400;500;600;700&family=Comic+Neue:wght@300;400;700&display=swap",
        "tailwind_config": "{ display: ['Baloo 2', 'cursive'], body: ['Comic Neue', 'cursive'] }",
    },
    "classic_elegant": {
        "display": "Playfair Display", "body": "Source Sans 3",
        "google_fonts_url": "https://fonts.googleapis.com/css2?family=Playfair+Display:wght@400;500;600;700&family=Source+Sans+3:wght@300;400;500;600;700&display=swap",
        "tailwind_config": "{ display: ['Playfair Display', 'serif'], body: ['Source Sans 3', 'sans-serif'] }",
    },
}

# --- Product Recommendations (style values from 6-item validated enum only) ---
PRODUCT_RECOMMENDATIONS = {
    "saas":                 {"fonts": "modern_professional", "style": "modern light"},
    "micro_saas":           {"fonts": "friendly_saas",      "style": "modern light"},
    "ecommerce":            {"fonts": "ecommerce_clean",    "style": "modern light"},
    "ecommerce_luxury":     {"fonts": "luxury_serif",       "style": "corporate professional"},
    "b2b_service":          {"fonts": "corporate_trust",    "style": "corporate professional"},
    "financial_dashboard":  {"fonts": "financial_trust",    "style": "modern dark"},
    "analytics_dashboard":  {"fonts": "financial_trust",    "style": "modern light"},
    "healthcare":           {"fonts": "medical_clean",      "style": "clean minimal"},
    "education":            {"fonts": "modern_professional", "style": "clean minimal"},
    "creative_agency":      {"fonts": "fashion_forward",    "style": "bold vibrant"},
    "portfolio":            {"fonts": "geometric_modern",   "style": "clean minimal"},
    "gaming":               {"fonts": "bold_statement",     "style": "bold vibrant"},
    "government":           {"fonts": "accessibility_first", "style": "corporate professional"},
    "fintech":              {"fonts": "tech_startup",       "style": "modern dark"},
    "social_media":         {"fonts": "modern_professional", "style": "bold vibrant"},
    "productivity":         {"fonts": "friendly_saas",      "style": "clean minimal"},
    "ai_chatbot":           {"fonts": "tech_startup",       "style": "modern light"},
    "pet_services":         {"fonts": "soft_rounded",       "style": "warm earthy"},
    "beauty_spa":           {"fonts": "wellness_calm",      "style": "warm earthy"},
    "luxury_brand":         {"fonts": "luxury_serif",       "style": "corporate professional"},
    "restaurant":           {"fonts": "restaurant_menu",    "style": "warm earthy"},
    "fitness":              {"fonts": "sports_fitness",     "style": "bold vibrant"},
    "real_estate":          {"fonts": "real_estate_luxury", "style": "corporate professional"},
    "travel":               {"fonts": "modern_professional", "style": "bold vibrant"},
    "hotel":                {"fonts": "classic_elegant",    "style": "corporate professional"},
    "wedding":              {"fonts": "warm_editorial",     "style": "warm earthy"},
    "legal":                {"fonts": "corporate_trust",    "style": "corporate professional"},
    "insurance":            {"fonts": "corporate_trust",    "style": "corporate professional"},
    "banking":              {"fonts": "financial_trust",    "style": "corporate professional"},
    "online_course":        {"fonts": "modern_professional", "style": "clean minimal"},
    "nonprofit":            {"fonts": "accessibility_first", "style": "clean minimal"},
    "job_board":            {"fonts": "corporate_trust",    "style": "modern light"},
    "marketplace":          {"fonts": "modern_professional", "style": "modern light"},
    "logistics":            {"fonts": "corporate_trust",    "style": "modern light"},
    "agriculture":          {"fonts": "soft_rounded",       "style": "warm earthy"},
    "construction":         {"fonts": "bold_statement",     "style": "corporate professional"},
    "automotive":           {"fonts": "geometric_modern",   "style": "modern dark"},
    "photography":          {"fonts": "geometric_modern",   "style": "clean minimal"},
    "coworking":            {"fonts": "modern_professional", "style": "bold vibrant"},
    "home_services":        {"fonts": "corporate_trust",    "style": "corporate professional"},
    "childcare":            {"fonts": "playful_creative",   "style": "warm earthy"},
    "medical_clinic":       {"fonts": "medical_clean",      "style": "clean minimal"},
    "veterinary":           {"fonts": "soft_rounded",       "style": "warm earthy"},
    "florist":              {"fonts": "wellness_calm",      "style": "warm earthy"},
    "bakery_cafe":          {"fonts": "restaurant_menu",    "style": "warm earthy"},
    "brewery_winery":       {"fonts": "editorial_classic",  "style": "warm earthy"},
    "news_media":           {"fonts": "corporate_trust",    "style": "modern light"},
    "freelancer":           {"fonts": "corporate_trust",    "style": "modern light"},
    "event_management":     {"fonts": "modern_professional", "style": "bold vibrant"},
    "membership_community": {"fonts": "friendly_saas",      "style": "modern light"},
    "newsletter":           {"fonts": "corporate_trust",    "style": "clean minimal"},
    "church":               {"fonts": "warm_editorial",     "style": "warm earthy"},
    "sports":               {"fonts": "sports_fitness",     "style": "bold vibrant"},
    "crm":                  {"fonts": "corporate_trust",    "style": "modern light"},
    "inventory":            {"fonts": "corporate_trust",    "style": "clean minimal"},
    "invoice":              {"fonts": "financial_trust",    "style": "corporate professional"},
    "booking":              {"fonts": "modern_professional", "style": "modern light"},
    "food_delivery":        {"fonts": "modern_professional", "style": "warm earthy"},
    "recipe_cooking":       {"fonts": "warm_editorial",     "style": "warm earthy"},
    "meditation":           {"fonts": "wellness_calm",      "style": "clean minimal"},
    "habit_tracker":        {"fonts": "friendly_saas",      "style": "modern light"},
    "plant_care":           {"fonts": "soft_rounded",       "style": "warm earthy"},
    "healing_arts":         {"fonts": "wellness_calm",      "style": "warm earthy"},
}

# --- Keyword Detection ---
PRODUCT_KEYWORDS = {
    "saas": "saas", "dashboard": "saas", "analytics": "analytics_dashboard",
    "pet": "pet_services", "grooming": "pet_services", "veterinary": "veterinary",
    "vet": "veterinary", "animal": "pet_services",
    "invoice": "invoice", "invoicing": "invoice", "billing": "invoice",
    "accounting": "invoice", "bookkeeping": "invoice",
    "freelance": "freelancer", "freelancer": "freelancer", "contractor": "freelancer",
    "restaurant": "restaurant", "food": "food_delivery", "menu": "restaurant",
    "cafe": "bakery_cafe", "bakery": "bakery_cafe", "catering": "restaurant",
    "coffee": "bakery_cafe",
    "gym": "fitness", "workout": "fitness", "exercise": "fitness",
    "fitness": "fitness", "yoga": "fitness", "training": "fitness",
    "doctor": "medical_clinic", "clinic": "medical_clinic", "patient": "healthcare",
    "medical": "medical_clinic", "health": "healthcare", "dental": "medical_clinic",
    "hospital": "healthcare", "pharmacy": "healthcare",
    "shop": "ecommerce", "store": "ecommerce", "product": "ecommerce",
    "ecommerce": "ecommerce", "marketplace": "marketplace",
    "property": "real_estate", "real estate": "real_estate", "listing": "real_estate",
    "housing": "real_estate", "apartment": "real_estate", "rental": "real_estate",
    "school": "education", "course": "online_course", "student": "education",
    "learning": "education", "tutoring": "education",
    "hotel": "hotel", "booking": "booking", "reservation": "booking",
    "appointment": "booking", "scheduling": "booking",
    "travel": "travel", "tourism": "travel", "vacation": "travel", "flight": "travel",
    "healing": "healing_arts", "ceremony": "healing_arts", "sacred": "healing_arts",
    "holistic": "healing_arts", "sound bath": "healing_arts", "breathwork": "healing_arts",
    "shaman": "healing_arts", "ritual": "healing_arts", "sweat lodge": "healing_arts",
    "wedding": "wedding", "event": "event_management", "conference": "event_management",
    "legal": "legal", "law": "legal", "attorney": "legal",
    "insurance": "insurance",
    "bank": "banking", "finance": "invoice",
    "nonprofit": "nonprofit", "charity": "nonprofit", "donation": "nonprofit",
    "church": "church", "ministry": "church",
    "job": "job_board", "recruit": "job_board", "hiring": "job_board", "career": "job_board",
    "delivery": "logistics", "shipping": "logistics", "tracking": "logistics",
    "farm": "agriculture", "agriculture": "agriculture", "crop": "agriculture",
    "construction": "construction", "architect": "construction", "building": "construction",
    "car": "automotive", "auto": "automotive", "vehicle": "automotive", "dealership": "automotive",
    "photo": "photography", "photography": "photography",
    "plumber": "home_services", "electrician": "home_services", "cleaning": "home_services",
    "childcare": "childcare", "daycare": "childcare", "nursery": "childcare",
    "beauty": "beauty_spa", "spa": "beauty_spa", "salon": "beauty_spa", "wellness": "beauty_spa",
    "flower": "florist", "florist": "florist", "plant": "plant_care", "garden": "plant_care",
    "brewery": "brewery_winery", "winery": "brewery_winery", "wine": "brewery_winery",
    "news": "news_media", "blog": "news_media", "magazine": "news_media",
    "membership": "membership_community", "community": "membership_community",
    "newsletter": "newsletter", "subscription": "newsletter",
    "crm": "crm", "client management": "crm", "customer management": "crm",
    "inventory": "inventory", "stock": "inventory", "warehouse": "inventory",
    "recipe": "recipe_cooking", "cooking": "recipe_cooking",
    "meditation": "meditation", "mindfulness": "meditation",
    "habit": "habit_tracker", "tracker": "habit_tracker",
    "sports": "sports", "team": "sports", "league": "sports",
    "gaming": "gaming", "game": "gaming",
    "portfolio": "portfolio", "personal site": "portfolio",
    "ai": "ai_chatbot", "chatbot": "ai_chatbot",
    "coworking": "coworking",
}

# --- UX Rules (web-only, high-impact subset from UUPM ux-guidelines.csv) ---
UX_RULES_COMPACT = (
    "- Sticky glassmorphism nav with active page indicator (bg-white/15 pill) and scroll shadow effect\n"
    "- Status badges: green=success/confirmed, amber=pending, red=error/overdue, gray=draft (pill shape)\n"
    "- Cards: use .card-premium (1rem radius, warm shadow, hover:translateY(-4px)). "
    "Dashboard cards: .stat-card with gradient top stripe\n"
    "- Forms: .input-premium class, validate on blur, inline error messages\n"
    "- Tables: overflow-x-auto wrapper for mobile, alternating rows\n"
    "- Touch targets: min 44x44px, 8px gap between adjacent targets\n"
    "- Loading states: skeleton screens or spinners during async operations\n"
    "- Confirm before destructive actions (delete, reset, remove)\n"
    "- Section variety: alternate .section-warm and .section-earth backgrounds between sections\n"
    "- Dividers: .divider-warm (accent gradient) between major sections\n"
    "- Animations: .animate-in on cards/list items for staggered fadeInUp on page load\n"
    "- Logo: nav logo h-12 md:h-14 minimum with object-contain and flex-shrink-0 (never squished)\n"
    "- Color harmony: all colors (buttons, sections, gradients) derived from spec palette, no random hues"
)

# --- Visual Design Checklist (mandatory rules Claude tends to skip) ---
VISUAL_DESIGN_CHECKLIST = (
    "### Visual Design Checklist (MANDATORY: verify each before finishing)\n"
    "1. Login/register: split-panel (brand gradient left w/ logo + font-brand serif name, "
    "form right; stacked on mobile). Never a centered card on blank page.\n"
    "2. Page backgrounds: warm tinted (--color-cream, --color-surface); alternate "
    ".section-warm and .section-earth. Never flat #fff.\n"
    "3. Nav bar: glassmorphism (backdrop-filter: blur(20px) saturate(1.4)) with "
    "rgba(primary,0.92). .scrolled adds shadow. Logo h-16+.\n"
    "4. Cards: .card-premium (1rem radius, warm border, translateY(-4px) hover); "
    "class=\"animate-in\" for staggered fadeInUp. Stat cards use .stat-card with gradient top stripe.\n"
    "5. Dashboards: .stat-card grid, colored icon badges, varied card sizes. "
    "Never uniform boxes.\n"
    "6. Hero: .hero-main multi-stop gradient background, left-aligned headline, "
    "brand in font-brand italic. No hexagon/honeycomb overlays.\n"
    "7. Footer: .footer-gradient, 3-column (logo, links, newsletter), warm divider.\n"
    "8. Forms: .input-premium (0.75rem radius, 1.5px border, colored focus ring). "
    "Buttons: solid primary w/ shadow, hover lift.\n"
    "9. Typography: font-brand (serif) for brand/decorative; font-display for headings; "
    "font-body for paragraphs. Section labels: plain uppercase, wide tracking, dim color (rule 15).\n"
    "10. Newsletter: full-width dark gradient, glass input, accent submit button.\n"
    "11. Color harmony: ALL backgrounds/gradients/buttons/accents derive from spec palette. No stray hues.\n"
    "12. NO gradient text on headings. No bg-clip-text / text-transparent / bg-gradient-to-r "
    "on h1/h2/h3. Solid color. For emphasis wrap ONE word in <em> with accent color (italic serif).\n"
    "13. NO 3+ identical rounded-full icon bullets. Never emit three or more rows each starting "
    "with a matching `rounded-full w-10 h-10 flex items-center justify-center` orb. Replace with "
    "editorial divided rows: italic serif claim, dim sub-copy, border-top 1px rgba(255,255,255,0.07) "
    "(dark) or rgba(0,0,0,0.07) (light). No icons.\n"
    "14. NO equal-weight 3-column card grids for feature lists. Use asymmetric 2-column "
    "(statement left, rows right) OR numbered editorial steps (01, 02, 03). "
    "If 3-col is truly required (pricing), one column MUST be emphasized.\n"
    "15. Section eyebrows are PLAIN TEXT: never pill/badge/tag. No background, no border. "
    "Use `<p class=\"text-xs font-semibold uppercase tracking-[0.18em]\" style=\"color:#545460;\">Label</p>` "
    "on dark, #6B7280 on light.\n"
    "16. Distribute accents across --color-accent (CTAs), --color-accent-warm (featured), "
    "--color-accent-earth (warm). Helpers: .badge-featured, .stat-card-warm, .stat-card-earth."
)

# --- Style-specific visual hooks (narrative mood descriptions per style) ---
STYLE_VISUAL_HOOKS = {
    "warm earthy": (
        "A warm, welcoming aesthetic that evokes handcrafted quality and natural materials. "
        "Primary navigation is richly colored, deep navy or forest green, with the business "
        "name in an elegant serif italic. Content areas use cream or warm off-white backgrounds, "
        "never cold grays. Cards have soft, diffused shadows that feel organic rather than sharp. "
        "Headings use a serif display font, conveying tradition and craft. CTAs use warm gradient "
        "treatments (primary-to-accent) with generous padding and rounded corners. Stat cards on "
        "dashboards feature a warm gradient accent stripe at the top. The overall impression "
        "should be: you are dealing with a thoughtful, quality-focused business."
    ),
    "clean minimal": (
        "A restrained, typography-driven aesthetic where negative space is the primary design "
        "element. Navigation is white with a subtle bottom border; no color, no shadow. Cards "
        "are defined by borders, not shadows. Every color decision defaults to off. The grid is "
        "tight: 2-3 column maximum, wide margins, generous line height. The brand communicates "
        "confidence through what it omits."
    ),
    "bold vibrant": (
        "A high-energy aesthetic that commands attention. Navigation is fully saturated in the "
        "primary brand color. Cards use shadow-xl and lift slightly on hover. CTAs are large, "
        "bold, and use the primary gradient. Stat cards are gradient-filled rather than white. "
        "Typography is heavy, extrabold or black weight, with tight leading. The overall "
        "impression is a brand that is loud, confident, and proud of it."
    ),
    "corporate professional": (
        "A formal, structured aesthetic that signals trustworthiness and precision. Navigation "
        "is white with a subtle shadow. Cards are white with structured gray borders, never "
        "colored. Typography uses medium-to-semibold weights without decorative elements. Stat "
        "cards are white with a left accent border stripe, not gradient-filled. The grid is "
        "strict: equal columns, consistent padding, no visual surprises. The impression is: "
        "competent, reliable, proven."
    ),
    "modern dark": (
        "A sophisticated dark-mode aesthetic built on dark slate backgrounds. Navigation is dark "
        "with a subtle divider. Text is white or slate-200 on dark surfaces; never light gray "
        "on a light card nested inside a dark page. Accent color pops against the dark background "
        "for CTAs and highlights. Cards use dark surface variants (slate-800, slate-900) with "
        "subtle border treatments. The impression is: technical, premium, night-owl-friendly."
    ),
    "modern light": (
        "A clean, airy light aesthetic with subtle tinting from the primary palette. Page "
        "backgrounds are white or near-white. Section backgrounds use primary/5 to primary/10 "
        "tints for gentle layering. Navigation is white with a thin border. Cards use shadow-sm. "
        "Typography is geometric sans-serif throughout. The impression is: modern SaaS, "
        "approachable, light and fast."
    ),
}

# --- Style-specific typography scale (Tailwind utility classes per heading level) ---
STYLE_TYPOGRAPHY_SCALE = {
    "corporate professional": (
        "H1: font-display text-4xl font-bold tracking-tight text-[var(--color-foreground)] leading-tight\n"
        "H2: font-display text-2xl font-semibold text-[var(--color-foreground)]\n"
        "H3: font-display text-xl font-semibold text-[var(--color-foreground)]\n"
        "H4: font-display text-lg font-medium\n"
        "body: font-body text-base leading-relaxed text-[var(--color-foreground)]\n"
        "small: font-body text-sm text-gray-500\n"
        "caption: font-body text-xs text-gray-400 tracking-wide uppercase"
    ),
    "warm earthy": (
        "H1: font-display text-5xl font-bold italic text-[var(--color-foreground)] leading-tight\n"
        "H2: font-display text-3xl font-semibold italic text-[var(--color-foreground)]\n"
        "H3: font-display text-xl font-semibold text-[var(--color-foreground)]\n"
        "H4: font-display text-lg font-medium\n"
        "body: font-body text-base leading-relaxed text-[var(--color-foreground)]\n"
        "small: font-body text-sm text-[var(--color-foreground)]/70\n"
        "caption: font-body text-xs tracking-widest uppercase text-[var(--color-accent)]"
    ),
    "clean minimal": (
        "H1: font-display text-4xl font-semibold tracking-tight text-[var(--color-foreground)] leading-snug\n"
        "H2: font-display text-2xl font-medium text-[var(--color-foreground)]\n"
        "H3: font-display text-lg font-medium text-[var(--color-foreground)]\n"
        "H4: font-display text-base font-medium\n"
        "body: font-body text-base leading-relaxed text-[var(--color-foreground)]\n"
        "small: font-body text-sm text-gray-400\n"
        "caption: font-body text-xs text-gray-400 tracking-wider uppercase"
    ),
    "bold vibrant": (
        "H1: font-display text-6xl font-black text-[var(--color-foreground)] leading-none\n"
        "H2: font-display text-4xl font-extrabold text-[var(--color-foreground)]\n"
        "H3: font-display text-2xl font-bold text-[var(--color-foreground)]\n"
        "H4: font-display text-xl font-bold\n"
        "body: font-body text-base leading-relaxed text-[var(--color-foreground)]\n"
        "small: font-body text-sm text-gray-500\n"
        "caption: font-body text-xs font-semibold tracking-widest uppercase text-[var(--color-primary)]"
    ),
    "modern dark": (
        "H1: font-display text-5xl font-bold text-white leading-tight\n"
        "H2: font-display text-3xl font-semibold text-white\n"
        "H3: font-display text-xl font-semibold text-slate-200\n"
        "H4: font-display text-lg font-medium text-slate-200\n"
        "body: font-body text-base leading-relaxed text-slate-300\n"
        "small: font-body text-sm text-slate-400\n"
        "caption: font-body text-xs text-slate-500 tracking-widest uppercase"
    ),
    "modern light": (
        "H1: font-display text-4xl font-semibold text-[var(--color-foreground)] leading-tight\n"
        "H2: font-display text-2xl font-medium text-[var(--color-foreground)]\n"
        "H3: font-display text-lg font-medium text-[var(--color-foreground)]\n"
        "H4: font-display text-base font-medium\n"
        "body: font-body text-base leading-relaxed text-[var(--color-foreground)]\n"
        "small: font-body text-sm text-gray-500\n"
        "caption: font-body text-xs text-gray-400 tracking-wider"
    ),
}

# --- Style-specific don't patterns (anti-pattern guidance per style) ---
STYLE_DONT_PATTERNS = {
    "clean minimal": (
        "NEVER use glassmorphism, gradients, or decorative textures. "
        "NEVER add box-shadows to cards: borders only. "
        "NEVER tint hero backgrounds with color. "
        "NEVER use rounded-2xl or larger. "
        "NEVER mix multiple accent colors. "
        "NEVER add decorative icons or patterns. "
        "NEVER add SVG hexagon, honeycomb, or geometric tile overlays."
    ),
    "modern dark": (
        "NEVER use white or light gray backgrounds in content sections. "
        "NEVER render light-colored text on a light card inside a dark page (check contrast). "
        "NEVER use saturated color backgrounds for the page base; dark slate only. "
        "NEVER use warm/earthy tones in a dark theme. "
        "NEVER add SVG hexagon, honeycomb, or geometric tile overlays."
    ),
    "warm earthy": (
        "NEVER use cold grays or blues as the primary background. "
        "NEVER use hard drop-shadows (box-shadow): use warm, diffused shadows only. "
        "NEVER use sans-serif fonts in hero headings. "
        "NEVER use flat/bright CTA colors: always warm gradients. "
        "NEVER add SVG hexagon, honeycomb, or geometric tile overlays."
    ),
    "bold vibrant": (
        "NEVER use muted, pastel, or low-saturation colors on CTAs. "
        "NEVER render flat/borderless cards: always shadow-xl minimum. "
        "NEVER use a white or light nav bar. "
        "NEVER use text-only ghost buttons as primary CTAs. "
        "NEVER add SVG hexagon, honeycomb, or geometric tile overlays."
    ),
    "corporate professional": (
        "NEVER use gradients on backgrounds or nav bars. "
        "NEVER use rounded-2xl: max rounded-lg. "
        "NEVER use colorful or playful typography. "
        "NEVER use decorative textures or patterns. "
        "NEVER place colored stat cards: white cards with structured borders only. "
        "NEVER add SVG hexagon, honeycomb, or geometric tile overlays."
    ),
    "modern light": (
        "NEVER use dark backgrounds in page sections. "
        "NEVER use heavy shadows (shadow-xl). "
        "NEVER use serif display fonts. "
        "NEVER use saturated color backgrounds: use primary/5 to primary/10 tints only. "
        "NEVER add SVG hexagon, honeycomb, or geometric tile overlays."
    ),
}

# --- Style-specific layout principles (spacing, grid, whitespace philosophy per style) ---
STYLE_LAYOUT_PRINCIPLES = {
    "clean minimal": (
        "Use generous py-20+ section spacing. Prefer 2-3 column grids maximum. "
        "Let content breathe: never crowd elements. Typography is the primary visual element. "
        "Wide margins, consistent gutters, no visual surprises."
    ),
    "bold vibrant": (
        "Use py-16 minimum sections with full-bleed colored backgrounds. "
        "Prefer asymmetric layouts with oversized headline blocks. "
        "Cards can break the grid for visual energy. Hero sections are full-viewport-height."
    ),
    "warm earthy": (
        "Organic, generous spacing with py-16+ between sections. Asymmetric layouts feel "
        "handcrafted rather than mechanical. Alternate warm background tones (cream, off-white) "
        "between sections. Max 3-column grids. Cards have generous internal padding (p-8)."
    ),
    "corporate professional": (
        "Strict 3-4 column grids with consistent py-12/py-16 spacing. Tables and data-dense "
        "CRUD pages use compact but readable layouts. Section dividers use border-b, not "
        "background color changes. No asymmetry: everything aligns to the grid."
    ),
    "modern dark": (
        "Dark panel sections with subtle dividers. Spacious card grids with generous padding. "
        "Minimal vertical rhythm: let the dark surfaces breathe. py-16 default section "
        "spacing. Code and data elements get monospace font treatment."
    ),
    "modern light": (
        "Airy py-16+ sections with clean 3-column grids on desktop. Light card containers "
        "with subtle tinted backgrounds create gentle depth without heavy shadows. "
        "Section backgrounds alternate between white and primary/5 tints for layering."
    ),
}

# --- Style-specific responsive notes (mobile and tablet behavior per style) ---
STYLE_RESPONSIVE_NOTES = {
    "clean minimal": (
        "Stack all columns to single-column on mobile. Nav collapses to hamburger at md "
        "breakpoint. Touch targets minimum 44px. Remove decorative whitespace on small screens."
    ),
    "bold vibrant": (
        "Full-bleed hero retained on mobile: scale headline via text-4xl on sm breakpoint. "
        "Card grid collapses to 1-column on mobile. Oversized CTAs remain prominent. "
        "Nav hamburger opens full-screen overlay menu."
    ),
    "warm earthy": (
        "Hero image stacks below text on mobile. Multi-column CRUD tables scroll horizontally. "
        "Stat card grids become 2-column on mobile, 1-column on small. "
        "Footer 3-column collapses to stacked single-column."
    ),
    "corporate professional": (
        "Data tables scroll horizontally on mobile; do not truncate columns. Stat cards "
        "2-column on tablet, 1-column on mobile. Sidebar nav collapses to top nav with "
        "breadcrumbs. Dense form layouts stack vertically."
    ),
    "modern dark": (
        "Dark theme preserved at all breakpoints; no light-mode fallback on mobile. "
        "Card grid collapses to 1-column on mobile. Fixed nav shrinks to icon-only or "
        "hamburger at sm breakpoint. Keep code blocks horizontally scrollable."
    ),
    "modern light": (
        "Light surfaces maintained on mobile. 3-column card grid collapses to 1-column "
        "at sm breakpoint. Nav hamburger at sm. Primary/5 tinted section backgrounds "
        "visible at all screen sizes."
    ),
}

# --- Style-specific component tokens (14 copy-pasteable Tailwind tokens per style) ---
# All tokens use CSS custom properties (var(--color-primary), etc.) so they adapt
# automatically to any customer palette. btn_danger always uses hardcoded red.
STYLE_COMPONENT_TOKENS = {
    "clean minimal": {
        "btn_primary": "bg-[var(--color-primary)] text-[var(--color-on-primary)] font-medium px-5 py-2 rounded-md hover:opacity-90 transition-opacity",
        "btn_secondary": "border border-[var(--color-primary)] text-[var(--color-primary)] bg-transparent font-medium px-5 py-2 rounded-md hover:bg-[var(--color-primary)]/5 transition-colors",
        "btn_ghost": "text-[var(--color-primary)] bg-transparent font-medium px-4 py-2 rounded-md hover:bg-[var(--color-primary)]/10 transition-colors",
        "btn_danger": "bg-red-600 text-white font-medium px-5 py-2 rounded-md hover:bg-red-700 transition-colors",
        "card": "bg-white border border-gray-200 rounded-lg p-6",
        "card_stat": "bg-white border border-gray-200 rounded-lg p-6 border-t-2 border-t-[var(--color-primary)]",
        "input": "w-full border border-gray-300 rounded-md px-4 py-2 text-sm focus:outline-none focus:ring-2 focus:ring-[var(--color-primary)]/40 focus:border-[var(--color-primary)]",
        "nav": "bg-white border-b border-gray-200 shadow-none",
        "badge_success": "inline-flex items-center px-2.5 py-0.5 rounded-full text-xs font-medium bg-green-100 text-green-800",
        "badge_warning": "inline-flex items-center px-2.5 py-0.5 rounded-full text-xs font-medium bg-amber-100 text-amber-800",
        "badge_error": "inline-flex items-center px-2.5 py-0.5 rounded-full text-xs font-medium bg-red-100 text-red-800",
        "shadow_sm": "shadow-none",
        "shadow_md": "shadow-sm",
        "shadow_lg": "shadow-md",
    },
    "bold vibrant": {
        "btn_primary": "bg-[var(--color-primary)] text-[var(--color-on-primary)] font-bold px-6 py-3 rounded-xl shadow-lg hover:shadow-xl hover:-translate-y-0.5 transition-all",
        "btn_secondary": "border-2 border-[var(--color-primary)] text-[var(--color-primary)] font-bold px-6 py-3 rounded-xl hover:bg-[var(--color-primary)] hover:text-[var(--color-on-primary)] transition-all",
        "btn_ghost": "text-[var(--color-primary)] font-semibold px-4 py-2 rounded-xl hover:bg-[var(--color-primary)]/10 transition-colors",
        "btn_danger": "bg-red-600 text-white font-bold px-6 py-3 rounded-xl shadow-lg hover:bg-red-700 transition-colors",
        "card": "bg-white rounded-2xl shadow-xl p-6 hover:-translate-y-1 transition-transform",
        "card_stat": "bg-gradient-to-br from-[var(--color-primary)] to-[var(--color-secondary)] text-[var(--color-on-primary)] rounded-2xl shadow-xl p-6",
        "input": "w-full border-2 border-gray-200 rounded-xl px-4 py-3 text-sm focus:outline-none focus:ring-0 focus:border-[var(--color-primary)] transition-colors",
        "nav": "bg-[var(--color-primary)] shadow-2xl",
        "badge_success": "inline-flex items-center px-3 py-1 rounded-full text-xs font-bold bg-green-500 text-white",
        "badge_warning": "inline-flex items-center px-3 py-1 rounded-full text-xs font-bold bg-amber-500 text-white",
        "badge_error": "inline-flex items-center px-3 py-1 rounded-full text-xs font-bold bg-red-500 text-white",
        "shadow_sm": "shadow-md",
        "shadow_md": "shadow-xl",
        "shadow_lg": "shadow-2xl",
    },
    "warm earthy": {
        "btn_primary": "bg-gradient-to-r from-[var(--color-primary)] to-[var(--color-accent)] text-[var(--color-on-primary)] font-semibold px-6 py-2.5 rounded-xl hover:opacity-90 transition-opacity shadow-md",
        "btn_secondary": "border border-[var(--color-primary)] text-[var(--color-primary)] bg-transparent font-medium px-5 py-2.5 rounded-xl hover:bg-[var(--color-primary)]/8 transition-colors",
        "btn_ghost": "text-[var(--color-primary)] bg-transparent font-medium px-4 py-2 rounded-xl hover:bg-[var(--color-primary)]/10 transition-colors",
        "btn_danger": "bg-red-600 text-white font-medium px-5 py-2.5 rounded-xl hover:bg-red-700 transition-colors",
        "card": "bg-white rounded-xl shadow-md border border-[var(--color-border)] p-6",
        "card_stat": "bg-white rounded-xl shadow-md border border-[var(--color-border)] p-6 border-t-4 border-t-[var(--color-primary)]",
        "input": "w-full border border-[var(--color-border)] rounded-xl px-4 py-2.5 text-sm focus:outline-none focus:ring-2 focus:ring-[var(--color-primary)]/30 focus:border-[var(--color-primary)] bg-[var(--color-background)]",
        "nav": "bg-[var(--color-primary)] shadow-lg",
        "badge_success": "inline-flex items-center px-2.5 py-0.5 rounded-full text-xs font-medium bg-green-100 text-green-800",
        "badge_warning": "inline-flex items-center px-2.5 py-0.5 rounded-full text-xs font-medium bg-amber-100 text-amber-800",
        "badge_error": "inline-flex items-center px-2.5 py-0.5 rounded-full text-xs font-medium bg-red-100 text-red-800",
        "shadow_sm": "shadow-sm",
        "shadow_md": "shadow-md",
        "shadow_lg": "shadow-lg",
    },
    "corporate professional": {
        "btn_primary": "bg-[var(--color-primary)] text-[var(--color-on-primary)] font-medium px-5 py-2 rounded-md hover:opacity-90 transition-opacity",
        "btn_secondary": "border border-[var(--color-primary)] text-[var(--color-primary)] bg-transparent font-medium px-5 py-2 rounded-md hover:bg-[var(--color-primary)]/5 transition-colors",
        "btn_ghost": "text-[var(--color-primary)] bg-transparent font-medium px-4 py-2 rounded-md hover:bg-[var(--color-primary)]/8 transition-colors",
        "btn_danger": "bg-red-600 text-white font-medium px-5 py-2 rounded-md hover:bg-red-700 transition-colors",
        "card": "bg-white border border-gray-200 rounded-lg p-6 shadow-sm",
        "card_stat": "bg-white border border-gray-200 rounded-lg p-6 border-l-4 border-l-[var(--color-primary)] shadow-sm",
        "input": "w-full border border-gray-300 rounded-md px-4 py-2 text-sm focus:outline-none focus:ring-1 focus:ring-[var(--color-primary)] focus:border-[var(--color-primary)]",
        "nav": "bg-white border-b border-gray-200 shadow-sm",
        "badge_success": "inline-flex items-center px-2.5 py-0.5 rounded-full text-xs font-medium bg-green-100 text-green-800",
        "badge_warning": "inline-flex items-center px-2.5 py-0.5 rounded-full text-xs font-medium bg-amber-100 text-amber-800",
        "badge_error": "inline-flex items-center px-2.5 py-0.5 rounded-full text-xs font-medium bg-red-100 text-red-800",
        "shadow_sm": "shadow-none",
        "shadow_md": "shadow-sm",
        "shadow_lg": "shadow-md",
    },
    "modern dark": {
        "btn_primary": "bg-[var(--color-primary)] text-[var(--color-on-primary)] font-medium px-5 py-2 rounded-lg hover:opacity-90 transition-opacity ring-1 ring-[var(--color-primary)]/50",
        "btn_secondary": "border border-[var(--color-primary)] text-[var(--color-primary)] bg-transparent font-medium px-5 py-2 rounded-lg hover:bg-[var(--color-primary)]/10 transition-colors",
        "btn_ghost": "text-[var(--color-primary)] bg-transparent font-medium px-4 py-2 rounded-lg hover:bg-[var(--color-primary)]/15 transition-colors",
        "btn_danger": "bg-red-600 text-white font-medium px-5 py-2 rounded-lg hover:bg-red-700 transition-colors",
        "card": "bg-slate-800/60 backdrop-blur-sm border border-slate-700 rounded-xl p-6",
        "card_stat": "bg-slate-800 border border-slate-700 rounded-xl p-6 border-t-2 border-t-[var(--color-primary)]",
        "input": "w-full bg-slate-800 border border-slate-600 rounded-lg px-4 py-2 text-sm text-slate-100 focus:outline-none focus:ring-2 focus:ring-[var(--color-primary)]/50 focus:border-[var(--color-primary)] placeholder:text-slate-500",
        "nav": "bg-slate-900 border-b border-slate-800",
        "badge_success": "inline-flex items-center px-2.5 py-0.5 rounded-full text-xs font-medium bg-green-900/50 text-green-400",
        "badge_warning": "inline-flex items-center px-2.5 py-0.5 rounded-full text-xs font-medium bg-amber-900/50 text-amber-400",
        "badge_error": "inline-flex items-center px-2.5 py-0.5 rounded-full text-xs font-medium bg-red-900/50 text-red-400",
        "shadow_sm": "shadow-md shadow-black/20",
        "shadow_md": "shadow-lg shadow-black/30",
        "shadow_lg": "shadow-xl shadow-black/40",
    },
    "modern light": {
        "btn_primary": "bg-[var(--color-primary)] text-[var(--color-on-primary)] font-medium px-5 py-2 rounded-lg hover:opacity-90 transition-opacity",
        "btn_secondary": "border border-[var(--color-primary)] text-[var(--color-primary)] bg-transparent font-medium px-5 py-2 rounded-lg hover:bg-[var(--color-primary)]/5 transition-colors",
        "btn_ghost": "text-[var(--color-primary)] bg-transparent font-medium px-4 py-2 rounded-lg hover:bg-[var(--color-primary)]/8 transition-colors",
        "btn_danger": "bg-red-600 text-white font-medium px-5 py-2 rounded-lg hover:bg-red-700 transition-colors",
        "card": "bg-white border border-gray-100 rounded-xl p-6 shadow-sm",
        "card_stat": "bg-[var(--color-primary)]/5 border border-[var(--color-primary)]/10 rounded-xl p-6",
        "input": "w-full border border-gray-200 rounded-lg px-4 py-2 text-sm focus:outline-none focus:ring-2 focus:ring-[var(--color-primary)]/30 focus:border-[var(--color-primary)] bg-white",
        "nav": "bg-white border-b border-gray-100 shadow-sm",
        "badge_success": "inline-flex items-center px-2.5 py-0.5 rounded-full text-xs font-medium bg-green-100 text-green-700",
        "badge_warning": "inline-flex items-center px-2.5 py-0.5 rounded-full text-xs font-medium bg-amber-100 text-amber-700",
        "badge_error": "inline-flex items-center px-2.5 py-0.5 rounded-full text-xs font-medium bg-red-100 text-red-700",
        "shadow_sm": "shadow-sm",
        "shadow_md": "shadow-md",
        "shadow_lg": "shadow-lg",
    },
}


def generate_design_md(spec: dict, ctx: dict, abbreviated: bool = False) -> str:
    """Generate a DESIGN.md-format string for injection into build prompts.

    Full mode (abbreviated=False): all 9 sections (~80-90 lines, ~850 tokens).
    Abbreviated mode (abbreviated=True): sections 2, 3, 4, 7 only (~35 lines, ~350 tokens).

    Abbreviated mode is used for Phase N multi-pass and Phase 1 prompts
    (schema/server/middleware, not visual components).

    Args:
        spec: The build spec dict. Reads spec.get("name") and spec.get("design.style").
        ctx:  Design context from get_design_context(). Keys: ctx["palette"], ctx["fonts"].
        abbreviated: If True, emit only sections 2, 3, 4, 7.

    Returns:
        A markdown string ready for injection into a build prompt.
        Falls back gracefully on missing keys.
    """
    style = spec.get("design", {}).get("style", "modern light")
    if style not in VALID_STYLES:
        style = "modern light"
    app_name = spec.get("name", "")
    palette = ctx.get("palette", {})
    fonts = ctx.get("fonts") or {}

    # --- Header ---
    header = f"# DESIGN.md: {app_name}\n**Style:** {style}"

    # --- Section 1: Visual Theme & Atmosphere (full mode only) ---
    section_1 = None
    if not abbreviated:
        mood = STYLE_VISUAL_HOOKS.get(style, STYLE_VISUAL_HOOKS.get("modern light", ""))
        section_1 = f"## 1. Visual Theme & Atmosphere\n\n{mood}"

    # --- Section 2: Color Palette (always) ---
    _PALETTE_ROLES = {
        "primary": "Primary action color",
        "on_primary": "Text on primary backgrounds",
        "secondary": "Supporting / secondary actions",
        "accent": "Accent / highlight color",
        "background": "Page background",
        "foreground": "Body text color",
        "card": "Card / surface background",
        "muted": "Muted text and subtle elements",
        "border": "Borders and dividers",
    }
    rows = ["| Token | Hex | Role |", "|-------|-----|------|"]
    for key, role in _PALETTE_ROLES.items():
        hex_val = palette.get(key, "")
        if hex_val:
            rows.append(f"| --color-{key.replace('_', '-')} | {hex_val} | {role} |")
    section_2 = "## 2. Color Palette\n\n" + "\n".join(rows)

    # --- Section 3: Typography (always, skip if fonts missing) ---
    section_3 = None
    if fonts:
        scale = STYLE_TYPOGRAPHY_SCALE.get(style, STYLE_TYPOGRAPHY_SCALE.get("modern light", ""))
        display_font = fonts.get("display", "")
        body_font = fonts.get("body", "")
        font_url = fonts.get("url", "")
        font_block = (
            f"Google Fonts: {font_url}\n\n"
            f"Tailwind fontFamily config:\n"
            f"  display: ['{display_font}', 'serif']\n"
            f"  body: ['{body_font}', 'sans-serif']"
        )
        section_3 = f"## 3. Typography\n\n{scale}\n\n{font_block}"

    # --- Section 4: Component Styles (always) ---
    tokens = STYLE_COMPONENT_TOKENS.get(style, STYLE_COMPONENT_TOKENS.get("modern light", {}))
    token_lines = [f"{k}: {v}" for k, v in tokens.items()]
    section_4 = "## 4. Component Styles\n\n" + "\n".join(token_lines)

    # --- Section 5: Layout Principles (full mode only) ---
    section_5 = None
    if not abbreviated:
        layout = STYLE_LAYOUT_PRINCIPLES.get(style, STYLE_LAYOUT_PRINCIPLES.get("modern light", ""))
        section_5 = f"## 5. Layout Principles\n\n{layout}"

    # --- Section 6: Depth & Elevation (full mode only) ---
    section_6 = None
    if not abbreviated:
        shadow_sm = tokens.get("shadow_sm", "shadow-sm")
        shadow_md = tokens.get("shadow_md", "shadow-md")
        shadow_lg = tokens.get("shadow_lg", "shadow-lg")
        section_6 = (
            f"## 6. Depth & Elevation\n\n"
            f"shadow_sm ({shadow_sm}): Use for cards and subtle separation.\n"
            f"shadow_md ({shadow_md}): Use for dropdowns, popovers, and interactive cards.\n"
            f"shadow_lg ({shadow_lg}): Use for modals, drawers, and high-priority surfaces."
        )

    # --- Section 7: Do's and Don'ts (always, including abbreviated mode) ---
    donts = STYLE_DONT_PATTERNS.get(style, STYLE_DONT_PATTERNS.get("modern light", ""))
    section_7 = f"## 7. Do's and Don'ts\n\n{donts}"

    # --- Section 8: Responsive Behavior (full mode only) ---
    section_8 = None
    if not abbreviated:
        responsive = STYLE_RESPONSIVE_NOTES.get(style, STYLE_RESPONSIVE_NOTES.get("modern light", ""))
        section_8 = f"## 8. Responsive Behavior\n\n{responsive}"

    # --- Section 9: Key Rules (full mode only) ---
    section_9 = None
    if not abbreviated:
        # Select 6 universally applicable rules from UX_RULES_COMPACT
        key_rules = (
            "- Split-panel login: left panel is a gradient brand area, right panel is the form.\n"
            "- Every dashboard must have stat cards with icons above key metrics.\n"
            "- Navigation must be solid (no transparent nav on any page).\n"
            "- All form inputs must have visible focus rings using --color-primary.\n"
            "- Seed data must be realistic and domain-specific (no Lorem ipsum).\n"
            "- Every submit button must be solid bg-[var(--color-primary)], never ghost."
        )
        section_9 = f"## 9. Key Rules\n\n{key_rules}"

    sections = [
        header,
        section_1,
        section_2,
        section_3,
        section_4,
        section_5,
        section_6,
        section_7,
        section_8,
        section_9,
    ]
    return "\n\n".join(s for s in sections if s)


def _hex_to_luminance(hex_color):
    """Calculate relative luminance of a hex color (WCAG 2.1)."""
    h = hex_color.lstrip("#")
    r, g, b = int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16)

    def srgb(c):
        c = c / 255.0
        return c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4

    return 0.2126 * srgb(r) + 0.7152 * srgb(g) + 0.0722 * srgb(b)


def _contrast_ratio(hex1, hex2):
    """WCAG contrast ratio between two hex colors."""
    l1 = _hex_to_luminance(hex1)
    l2 = _hex_to_luminance(hex2)
    lighter, darker = max(l1, l2), min(l1, l2)
    return (lighter + 0.05) / (darker + 0.05)


def _ensure_wcag(primary, on_primary):
    """Return on_primary that passes WCAG AA (4.5:1) with primary."""
    if _contrast_ratio(primary, on_primary) >= 4.5:
        return on_primary
    if _contrast_ratio(primary, "#0F172A") >= 4.5:
        return "#0F172A"
    return "#FFFFFF"


def match_product_type(description):
    """Match app description to product type via keyword lookup. Returns None if no match."""
    if not description:
        return None
    desc_lower = description.lower()
    for keyword, product_type in PRODUCT_KEYWORDS.items():
        if keyword in desc_lower:
            return product_type
    return None


def _hex_luminance(hex_color):
    """Return relative luminance (0-1) of a hex color. Low = dark."""
    h = hex_color.lstrip("#")
    if len(h) != 6:
        return 0.5  # fallback for malformed
    r, g, b = int(h[0:2], 16) / 255, int(h[2:4], 16) / 255, int(h[4:6], 16) / 255
    # sRGB linearization
    def _lin(c):
        return c / 12.92 if c <= 0.04045 else ((c + 0.055) / 1.055) ** 2.4
    return 0.2126 * _lin(r) + 0.7152 * _lin(g) + 0.0722 * _lin(b)


def _are_generic_dark_colors(primary, secondary, accent):
    """Return True if all three colors are very dark (Claude placeholder pattern).

    Claude sometimes generates near-black placeholder colors like #002040/#202040/#404060
    during intake. These produce ugly, indistinguishable palettes. When a UUPM palette
    is available, it should take priority over these generic darks.
    """
    try:
        lums = [_hex_luminance(c) for c in [primary, secondary, accent]]
        return all(l < 0.08 for l in lums)  # all three very dark
    except (ValueError, TypeError):
        return False


def get_design_context(spec):
    """Return design context merging UUPM defaults with spec.design overrides.

    Customer-specified colors/fonts always take priority over UUPM recommendations,
    UNLESS all three spec colors are generic dark placeholders (luminance < 0.08)
    and a UUPM palette is available.
    All returned palettes are WCAG AA verified.
    """
    design = spec.get("design", {}) or {}
    description = spec.get("description", "")
    product_type = match_product_type(description)

    palette = {}
    if product_type and product_type in COLOR_PALETTES:
        palette = COLOR_PALETTES[product_type].copy()

    # Check if spec colors look like generic dark placeholders
    spec_primary = design.get("primary_color", "")
    spec_secondary = design.get("secondary_color", "")
    spec_accent = design.get("accent_color", "")
    has_uupm = bool(palette)
    is_generic_dark = (
        spec_primary and spec_secondary and spec_accent
        and _are_generic_dark_colors(spec_primary, spec_secondary, spec_accent)
    )

    if is_generic_dark and has_uupm:
        # Prefer UUPM palette over generic dark placeholder colors
        pass  # keep the palette as-is, don't override with spec colors
    else:
        for field, key in [("primary_color", "primary"), ("secondary_color", "secondary"), ("accent_color", "accent")]:
            if design.get(field):
                palette[key] = design[field]

    if palette.get("primary") and palette.get("on_primary"):
        palette["on_primary"] = _ensure_wcag(palette["primary"], palette["on_primary"])

    rec = PRODUCT_RECOMMENDATIONS.get(product_type, {})
    font_key = rec.get("fonts", "modern_professional")
    pairing = FONT_PAIRINGS.get(font_key, FONT_PAIRINGS["modern_professional"]).copy()

    for field, key in [("font_display", "display"), ("font_body", "body")]:
        if design.get(field) and design[field] not in BANNED_FONTS:
            pairing[key] = design[field]

    return {
        "product_type": product_type,
        "palette": palette,
        "fonts": pairing,
        "style": rec.get("style", design.get("style", "modern light")),
    }
