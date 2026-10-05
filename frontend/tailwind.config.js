/** @type {import('tailwindcss').Config} */
export default {
  content: ['./index.html', './src/**/*.{ts,tsx}'],
  theme: {
    extend: {
      colors: {
        campus: {
          50: '#f0f9ff',
          100: '#e0f2fe',
          200: '#bae6fd',
          500: '#0284c7',
          600: '#0369a1',
          700: '#075985',
          900: '#0c2340',
        },
        // Bay states. Kept in sync with app/db/models.py BAY_* constants.
        bay: {
          free: '#22c55e',
          allotted: '#f59e0b',
          occupied: '#0f172a',
          blocked: '#dc2626',
          unknown: '#94a3b8',
        },
      },
      fontFamily: {
        sans: ['Inter', 'system-ui', '-apple-system', 'Segoe UI', 'sans-serif'],
      },
      boxShadow: {
        panel: '0 10px 40px -12px rgba(12, 35, 64, 0.25)',
      },
    },
  },
  plugins: [],
}
