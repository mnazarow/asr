// ESLint для веб-интерфейса (server/asrhub/web): плоская настройка ESLint 9.
//
//   npx eslint@9 server/asrhub/web            — в каталоге проекта
//
// Правила — те, что ловят настоящие ошибки, а не стиль: необъявленные и
// неиспользуемые имена, недостижимый код, дубли ключей, eval и
// встроенные обработчики через строку. Стиль кода — как есть.
export default [
  {
    files: ["server/asrhub/web/**/*.js"],
    languageOptions: {
      ecmaVersion: 2022,
      sourceType: "script",
      globals: {
        window: "readonly", document: "readonly", location: "readonly",
        history: "readonly", navigator: "readonly", console: "readonly",
        fetch: "readonly", WebSocket: "readonly", URL: "readonly",
        URLSearchParams: "readonly", Blob: "readonly", FormData: "readonly",
        FileReader: "readonly", setTimeout: "readonly", clearTimeout: "readonly",
        setInterval: "readonly", clearInterval: "readonly", requestAnimationFrame: "readonly",
        cancelAnimationFrame: "readonly", AbortController: "readonly", Audio: "readonly",
        AudioContext: "readonly", MediaRecorder: "readonly", ResizeObserver: "readonly",
        IntersectionObserver: "readonly", MutationObserver: "readonly",
        getComputedStyle: "readonly", matchMedia: "readonly", performance: "readonly",
        TextDecoder: "readonly", TextEncoder: "readonly", atob: "readonly", btoa: "readonly",
        crypto: "readonly", Event: "readonly", CustomEvent: "readonly", Node: "readonly",
        HTMLElement: "readonly", Image: "readonly", alert: "readonly", confirm: "readonly",
        prompt: "readonly", Intl: "readonly", queueMicrotask: "readonly",
        structuredClone: "readonly", innerWidth: "readonly", innerHeight: "readonly",
        devicePixelRatio: "readonly", sessionStorage: "readonly", screen: "readonly",
        DOMParser: "readonly", XMLHttpRequest: "readonly", Charts: "readonly",
        File: "readonly", Response: "readonly", Request: "readonly", Headers: "readonly",
        ClipboardItem: "readonly", OffscreenCanvas: "readonly", ImageData: "readonly",
        webkitAudioContext: "readonly", Worker: "readonly", BroadcastChannel: "readonly",
        EventSource: "readonly", Notification: "readonly", print: "readonly",
        scrollTo: "readonly", open: "readonly", close: "readonly", CSS: "readonly",
      },
    },
    rules: {
      "no-undef": "error",
      "no-unused-vars": ["error", { args: "none", caughtErrors: "none",
                                     varsIgnorePattern: "^_" }],
      "no-unreachable": "error",
      "no-dupe-keys": "error",
      "no-dupe-else-if": "error",
      "no-duplicate-case": "error",
      "no-self-assign": "error",
      "no-redeclare": "error",
      "no-eval": "error",
      "no-implied-eval": "error",
      "no-new-func": "error",
      "no-script-url": "error",
      "no-unsafe-finally": "error",
      "no-constant-condition": ["error", { checkLoops: false }],
      "use-isnan": "error",
      "valid-typeof": "error",
    },
  },
];
