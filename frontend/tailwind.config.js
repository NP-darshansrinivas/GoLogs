/** @type {import('tailwindcss').Config} */
export default {
  content: ["./index.html", "./src/**/*.{js,ts,jsx,tsx}"],
  theme: {
    extend: {
      colors: {
        // Deep charcoal-slate, not pure black — the "night shift analyst
        // workstation" base, deliberately cool rather than warm.
        bg: {
          primary: "#12151b",
          surface: "#1a1e26",
          elevated: "#232833",
          inset: "#0d1015",
        },
        border: {
          DEFAULT: "#2b313d",
          strong: "#3a4150",
        },
        text: {
          primary: "#e7e9ed",
          secondary: "#8b93a3",
          muted: "#5c6472",
        },
        // Cold analytical blue: informational, read-only, AUTO_APPROVE.
        accent: {
          DEFAULT: "#5b8cff",
          soft: "#5b8cff1a",
        },
        // Amber is reserved exclusively for the confirmation gate — the
        // one warm note in an otherwise cool palette, so it reads as
        // unmistakably significant every time it appears.
        gate: {
          DEFAULT: "#e8a33d",
          soft: "#e8a33d1a",
          strong: "#f0b65e",
        },
        danger: {
          DEFAULT: "#e2584f",
          soft: "#e2584f1a",
        },
        success: {
          DEFAULT: "#4caf7d",
          soft: "#4caf7d1a",
        },
      },
      fontFamily: {
        // IBM Plex: designed for technical/engineering documentation —
        // reads as "investigation tooling," not a generic marketing sans.
        sans: ["'IBM Plex Sans'", "system-ui", "sans-serif"],
        mono: ["'IBM Plex Mono'", "ui-monospace", "monospace"],
      },
      boxShadow: {
        gate: "0 0 0 1px #e8a33d33, 0 8px 32px -8px #e8a33d40",
      },
    },
  },
  plugins: [],
};
