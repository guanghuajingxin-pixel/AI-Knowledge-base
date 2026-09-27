/**
 * MinerU 解析产物的图片引用是相对路径（images/xxx.jpg），分段内容按原样存储。
 * 渲染时改写为后端代理 URL（<img> 无法携带 Authorization 头，JWT 走 ?token=）。
 */

/** 把 HTML 中 src 为 MinerU 相对引用（images/... 或 ./images/...）的 img 重写为代理 URL。 */
export function rewriteChunkImages(html: string, libId: number | string, docId: string): string {
  if (!html || !html.includes('<img')) return html
  const token = localStorage.getItem('kb_token') || ''
  const tpl = document.createElement('template')
  tpl.innerHTML = html
  tpl.content.querySelectorAll('img').forEach((img) => {
    const src = img.getAttribute('src') || ''
    if (!/^(?:\.\/)?images\//.test(src)) return
    const name = src.split('/').pop()!
    img.setAttribute(
      'src',
      `/api/v1/document-libraries/${libId}/documents/${docId}/images/${name}?token=${encodeURIComponent(token)}`,
    )
    img.setAttribute('loading', 'lazy')
  })
  return tpl.innerHTML
}
