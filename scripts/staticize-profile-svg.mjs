import { readFile, writeFile } from "node:fs/promises";

const [
  inputPath = "profile-3d-contrib/profile-physical-ai.svg",
  outputPath = "profile-3d-contrib/profile-physical-ai-static.svg",
] = process.argv.slice(2);

const animatedSvg = await readFile(inputPath, "utf8");
const staticSvg = animatedSvg
  .replace(/<animate(?:Transform)?\b[^>]*>[\s\S]*?<\/animate(?:Transform)?>/g, "")
  .replace(/<animate(?:Transform)?\b[^>]*\/>/g, "");

if (staticSvg.includes("<animate")) {
  throw new Error("Animated SVG elements remain after static conversion.");
}

await writeFile(outputPath, staticSvg, "utf8");

