import { useMemo } from 'react';
import { qrCodeShape } from '../../utils/qrCode';

/**
 * A QR code as inline SVG. Always black on white, whatever the theme: a phone
 * camera reading it off a screen needs the contrast and the light quiet zone.
 */
export function QrCode({ value, label }: { value: string; label: string }) {
  const { size, path } = useMemo(() => qrCodeShape(value), [value]);

  return (
    <svg className="qr-code" viewBox={`0 0 ${size} ${size}`} role="img" aria-label={label} shapeRendering="crispEdges">
      <rect width={size} height={size} fill="#ffffff" />
      <path d={path} fill="#000000" />
    </svg>
  );
}
