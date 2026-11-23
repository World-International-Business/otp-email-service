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
          50: '#eef1fa', 100: '#dde2f4', 200: '#bfc7e8', 300: '#9aa5d4',
          400: '#6f7ec0', 500: '#5060a8', 600: '#424f8c', 700: '#364170',
          800: '#2c355c', 900: '#252c4a', 950: '#161b31',
        },
        accent: {
          50: '#fef5e7', 100: '#fde7c2', 200: '#fbd088', 300: '#f7b34a',
          400: '#f08810', 500: '#d9780d', 600: '#b55f09', 700: '#8f4b09',
          800: '#743d0d', 900: '#5f330d',
        },
      },
      boxShadow: {
        soft: '0 2px 12px rgba(80,96,168,.14)',
      },
    },
  },
  plugins: [],
}
