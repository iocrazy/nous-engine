const localMedia = /^\/(?:files\/images\/|api\/v1\/tasks\/gallery\/)/

export function isLocalGalleryMedia(url: string) {
  return localMedia.test(url)
}

export function galleryPreviewUrl(url?: string) {
  return url && isLocalGalleryMedia(url) ? `${url}${url.includes('?') ? '&' : '?'}preview=1` : url
}
