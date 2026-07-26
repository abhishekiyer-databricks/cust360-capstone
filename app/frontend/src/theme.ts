import { createTheme, MantineColorsTuple } from "@mantine/core";

// Databricks palette (same tokens used in the earlier ops-agent app):
//   navy #1B3139 (sidebar/ink), lava #FF3621 (primary/active).
// Mantine wants a 10-shade tuple per custom color; these are hand-tuned ramps.
const lava: MantineColorsTuple = [
  "#ffe9e4",
  "#ffd2c8",
  "#ffa494",
  "#ff735c",
  "#ff4a2e",
  "#ff3621", // index 5 — primary shade
  "#f52c17",
  "#da2110",
  "#c2180b",
  "#a90c00",
];

const navy: MantineColorsTuple = [
  "#eef3f5",
  "#dbe3e6",
  "#b3c4ca",
  "#89a3ac",
  "#688893",
  "#537785",
  "#456a79",
  "#1B3139", // deep navy for the shell
  "#152830",
  "#0d1c22",
];

export const theme = createTheme({
  primaryColor: "lava",
  primaryShade: 5,
  colors: { lava, navy },
  fontFamily:
    "-apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, Arial, sans-serif",
  defaultRadius: "md",
});
