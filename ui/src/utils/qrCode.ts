import qrcode from 'qrcode-generator';

/** Light modules a scanner needs on every side of the code (four, per the QR spec). */
export const QR_QUIET_ZONE = 4;

export interface QrCodeShape {
  /** Modules per side, quiet zone included: the SVG viewBox is `0 0 size size`. */
  size: number;
  /** The dark modules as one SVG path, one unit per module. */
  path: string;
}

/** The QR code for `text` (error correction M, smallest version that fits). */
export function qrCodeShape(text: string): QrCodeShape {
  const code = qrcode(0, 'M');
  code.addData(text);
  code.make();

  const modules = code.getModuleCount();
  const squares: string[] = [];
  for (let row = 0; row < modules; row += 1) {
    for (let column = 0; column < modules; column += 1) {
      if (code.isDark(row, column)) {
        squares.push(`M${column + QR_QUIET_ZONE} ${row + QR_QUIET_ZONE}h1v1h-1z`);
      }
    }
  }
  return { size: modules + QR_QUIET_ZONE * 2, path: squares.join('') };
}
