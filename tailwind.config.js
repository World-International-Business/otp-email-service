/** @type {import('tailwindcss').Config} */
module.exports = {
  content: ["./src/**/*.py"],
  theme: {
    extend: {
      fontFamily: {
        sans: ['Inter', 'system-ui', 'sans-serif'],
        display: ['Moncera', '"Space Grotesk"', 'Inter', 'sans-serif'],
        mono: ['"JetBrains Mono"', 'ui-monospace', 'monospace'],
      },
      colors: {
        brand: {
          50: '#eef0fc', 100: '#dfe3f9', 200: '#c4cbf1', 300: '#a1abe6',
          400: '#7b86d8', 500: '#5b63c8', 600: '#4a51b8', 700: '#3c4197',
          800: '#31357a', 900: '#2a2d5f', 950: '#191b3d',
        },
        accent: {
          50: '#fef7e8', 100: '#fdecc4', 200: '#fbd98a', 300: '#f9c04e',
          400: '#f5a623', 500: '#e88f0e', 600: '#c26f09', 700: '#9b540b',
          800: '#7d4310', 900: '#693810',
        },
      },
      boxShadow: {
        soft: '0 2px 12px rgba(91,99,200,.14)',
      },
    },
  },
  plugins: [],
}
