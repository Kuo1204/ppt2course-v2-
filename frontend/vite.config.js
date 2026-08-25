import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'

// https://vite.dev/config/
export default defineConfig({
  // Relative asset URLs: this app is deployed under a per-account path
  // prefix by the hosting platform's reverse proxy (stripped before it
  // reaches this container). An absolute base ("/") bakes asset URLs like
  // "/assets/index-xxx.js" into index.html, which the browser resolves
  // against the platform's domain root instead of this service -- the page
  // loads (HTTP 200) but every JS/CSS request 404s and the screen is blank.
  base: './',
  plugins: [react()],
})
