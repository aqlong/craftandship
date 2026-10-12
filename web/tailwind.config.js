/** @type {import('tailwindcss').Config} */
module.exports = {
  content: ['./views/**/*.ejs', './public/**/*.js', './src/styles/input.css'],
  darkMode: 'class',
  theme: {
    extend: {
      colors: {
        // Primary: Harbor lantern (warm gold) replacing the legacy ocean cyan.
        // Admin dark-mode is the only consumer of this palette now (public
        // pages ride the .harbor scope in /brand-harbor/tokens.css). Warm gold
        // reads as the same brand identity as the warm-paper public site.
        brand: {
          DEFAULT: '#C68A3A',     // lantern
          hover: '#A87328',
          dark: '#8E5A1F',        // lantern-deep
          glow: '#E5B470',
          subtle: '#2D2218',
          // Supporting palette
          sky: '#A89B7A',         // in-progress states (warm muted)
          teal: '#6E8A7A',        // success/scale (companion sage)
          rose: '#C47A7A',        // failed states (warm dusty rose)
          amber: '#C68A3A',       // accent (same as default)
        },
        // Warm dark surfaces: still dark-mode, but no blue undertone
        surface: {
          DEFAULT: '#1A1612',
          2: '#221C16',
          3: '#2A2218',
        },
        bg: {
          DEFAULT: '#150F09',
          card: '#1A1612',
        },
        border: {
          DEFAULT: 'rgba(251, 241, 220, 0.08)',
          hover: 'rgba(251, 241, 220, 0.16)',
        },
        // Warm-tinted text: no blue tint
        muted: '#A89B85',
        subtle: '#7E6E55',
        // Retained for admin/status pages
        vivid: {
          violet: '#8b5cf6',
          cyan: '#06b6d4',
        },
        secondary: '#3b82f6',
        accent: '#f59e0b',
      },
      fontFamily: {
        // Editorial serif for all headings: craft-associated, not SaaS-generic
        display: ['"Instrument Serif"', 'Georgia', '"Times New Roman"', 'serif'],
        // Clean body type: stays out of the way
        sans: ['Inter', '-apple-system', 'BlinkMacSystemFont', 'Segoe UI', 'sans-serif'],
        mono: ['"JetBrains Mono"', '"SF Mono"', '"Fira Code"', 'monospace'],
      },
      maxWidth: {
        site: '1200px',
      },
      fontSize: {
        'display-xl': ['clamp(3rem, 5.5vw, 5rem)', { lineHeight: '1.04', letterSpacing: '-0.025em' }],
        'display-lg': ['clamp(2.25rem, 4vw, 3.5rem)', { lineHeight: '1.1', letterSpacing: '-0.02em' }],
        'display-md': ['clamp(1.75rem, 2.5vw, 2.25rem)', { lineHeight: '1.2', letterSpacing: '-0.015em' }],
      },
      borderRadius: {
        xl: '12px',
        '2xl': '16px',
        '3xl': '24px',
      },
      boxShadow: {
        card: '0 2px 12px rgba(0,0,0,0.4)',
        elevated: '0 8px 32px rgba(0,0,0,0.5), 0 0 0 1px rgba(255,255,255,0.05)',
        'brand-sm': '0 0 16px rgba(1,135,160,0.15)',
        'brand-md': '0 0 28px rgba(1,135,160,0.2)',
        'glow-multi': '0 0 32px rgba(1,135,160,0.15)',
        'glow-ocean': '0 0 24px rgba(1,135,160,0.2)',
      },
      animation: {
        'typing-bounce': 'typing-bounce 1.4s infinite',
        'fade-in': 'fade-in 0.3s ease-out',
        'slide-up': 'slide-up 0.4s ease-out',
        shimmer: 'shimmer 2.5s infinite linear',
        'pulse-soft': 'pulse-soft 3s ease-in-out infinite',
      },
      keyframes: {
        'typing-bounce': {
          '0%, 60%, 100%': { transform: 'translateY(0)' },
          '30%': { transform: 'translateY(-6px)' },
        },
        'fade-in': {
          from: { opacity: '0' },
          to: { opacity: '1' },
        },
        'slide-up': {
          from: { opacity: '0', transform: 'translateY(16px)' },
          to: { opacity: '1', transform: 'translateY(0)' },
        },
        shimmer: {
          '0%': { backgroundPosition: '-200% 0' },
          '100%': { backgroundPosition: '200% 0' },
        },
        'pulse-soft': {
          '0%, 100%': { opacity: '1' },
          '50%': { opacity: '0.7' },
        },
      },
    },
  },
  plugins: [],
};
