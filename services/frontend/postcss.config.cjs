// Required so Vite runs Tailwind (+ autoprefixer) over the @tailwind directives
// in src/styles/tokens.css. Without this, Tailwind never compiles and the
// utility classes the components use produce no CSS.
module.exports = {
  plugins: {
    tailwindcss: {},
    autoprefixer: {},
  },
};
