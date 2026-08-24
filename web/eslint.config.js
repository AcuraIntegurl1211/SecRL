// The platform keeps lint intentionally dependency-light. TypeScript's strict
// build is the type/language gate; this config makes the pinned ESLint command
// deterministic while ignoring generated output.
export default [
  {
    ignores: ["dist/**", "node_modules/**"],
  },
];
