export type ImageSize = { width: number; height: number }
export type ImageOffset = { x: number; y: number }

/** Shared image geometry. Scale 1 always means fitted, independent of source resolution. */
export function fittedImage(image: ImageSize | undefined, space: ImageSize): ImageSize {
  if (!image?.width || !image.height || !space.width || !space.height) return { width: 0, height: 0 }
  const scale = Math.min(space.width / image.width, space.height / image.height)
  return { width: image.width * scale, height: image.height * scale }
}
export function boundImagePan(pan: ImageOffset, fit: ImageSize, zoom: number): ImageOffset {
  const x = Math.max(0, fit.width * (zoom - 1) / 2), y = Math.max(0, fit.height * (zoom - 1) / 2)
  return { x: x ? Math.max(-x, Math.min(x, pan.x)) : 0, y: y ? Math.max(-y, Math.min(y, pan.y)) : 0 }
}
export function zoomImage(pan: ImageOffset, scale: number, multiplier: number, anchor: ImageOffset, fit: ImageSize) {
  const zoom = Math.max(1, Math.min(16, scale * multiplier)), ratio = zoom / scale
  return { zoom, pan: boundImagePan({ x: anchor.x + (pan.x - anchor.x) * ratio,
    y: anchor.y + (pan.y - anchor.y) * ratio }, fit, zoom) }
}
