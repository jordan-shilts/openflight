// Renders public/icons from public/openflight-icon-black.svg. Run from ui/ after
// the logo or the dark background colour changes: node scripts/generate-app-icons.mjs
import { mkdirSync, readFileSync, writeFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { chromium } from '@playwright/test';

const PUBLIC_DIR = fileURLToPath(new URL('../public/', import.meta.url));
const ICONS_DIR = `${PUBLIC_DIR}icons/`;
/** Dark theme --color-bg (src/index.css), also the manifest's background_color. */
const BACKGROUND = '#0e0f10';
/** The source paints its own black square; the icons use BACKGROUND instead. */
const SOURCE_BACKDROP = '<rect width="600" height="600" fill="#000000"/>';
/** artWidth is the share of the icon the logo spans; a maskable icon may be cropped to its middle 80%. */
const ICONS = [
  { file: 'icon-192.png', size: 192, artWidth: 0.86 },
  { file: 'icon-512.png', size: 512, artWidth: 0.86 },
  { file: 'icon-maskable-512.png', size: 512, artWidth: 0.7 },
  { file: 'apple-touch-icon.png', size: 180, artWidth: 0.86 },
];

const source = readFileSync(`${PUBLIC_DIR}openflight-icon-black.svg`, 'utf8');
if (!source.includes(SOURCE_BACKDROP)) {
  throw new Error('openflight-icon-black.svg no longer starts with its black backdrop; update SOURCE_BACKDROP.');
}
const logo = `data:image/svg+xml;base64,${Buffer.from(source.replace(SOURCE_BACKDROP, '')).toString('base64')}`;

const browser = await chromium.launch();
try {
  const page = await browser.newPage();
  const rendered = await page.evaluate(
    async ({ logo, background, icons }) => {
      const image = new Image();
      image.src = logo;
      await image.decode();

      const RASTER = 2048;
      const raster = document.createElement('canvas');
      raster.width = RASTER;
      raster.height = RASTER;
      const rasterContext = raster.getContext('2d');
      rasterContext.drawImage(image, 0, 0, RASTER, RASTER);

      const alpha = rasterContext.getImageData(0, 0, RASTER, RASTER).data;
      const box = { left: RASTER, top: RASTER, right: 0, bottom: 0 };
      for (let y = 0; y < RASTER; y += 1) {
        for (let x = 0; x < RASTER; x += 1) {
          if (alpha[(y * RASTER + x) * 4 + 3] === 0) continue;
          box.left = Math.min(box.left, x);
          box.right = Math.max(box.right, x + 1);
          box.top = Math.min(box.top, y);
          box.bottom = Math.max(box.bottom, y + 1);
        }
      }
      const artWidth = box.right - box.left;
      const artHeight = box.bottom - box.top;

      return icons.map(({ file, size, artWidth: share }) => {
        const canvas = document.createElement('canvas');
        canvas.width = size;
        canvas.height = size;
        const context = canvas.getContext('2d');
        context.fillStyle = background;
        context.fillRect(0, 0, size, size);
        const width = size * share;
        const height = (width * artHeight) / artWidth;
        const [left, top] = [(size - width) / 2, (size - height) / 2];
        context.imageSmoothingQuality = 'high';
        context.drawImage(raster, box.left, box.top, artWidth, artHeight, left, top, width, height);
        return { file, base64: canvas.toDataURL('image/png').split(',')[1] };
      });
    },
    { logo, background: BACKGROUND, icons: ICONS }
  );

  mkdirSync(ICONS_DIR, { recursive: true });
  for (const { file, base64 } of rendered) {
    writeFileSync(`${ICONS_DIR}${file}`, Buffer.from(base64, 'base64'));
    console.log(`wrote public/icons/${file}`);
  }
} finally {
  await browser.close();
}
