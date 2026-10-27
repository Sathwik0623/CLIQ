/// <reference types="vite/client" />

// Allow side-effect imports of CSS (and CSS from node_modules like
// 'xterm/css/xterm.css'). Vite handles these at build time; this declaration
// stops the TS language server from reporting TS2882 for the import.
declare module '*.css';
