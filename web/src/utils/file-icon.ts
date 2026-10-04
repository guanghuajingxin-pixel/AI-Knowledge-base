// 文件类型图标：按文件扩展名自动匹配 assets/file-icons 下的类型图标；
// 未识别的扩展名回退为纯文本（txt）图标。
import audioIcon from '@/assets/file-icons/audio.png'
import codeIcon from '@/assets/file-icons/code.png'
import csvIcon from '@/assets/file-icons/csv.png'
import docxIcon from '@/assets/file-icons/docx.png'
import jpgIcon from '@/assets/file-icons/jpg.png'
import jsonIcon from '@/assets/file-icons/json.png'
import mdIcon from '@/assets/file-icons/md.png'
import pdfIcon from '@/assets/file-icons/pdf.png'
import pngIcon from '@/assets/file-icons/png.png'
import pptxIcon from '@/assets/file-icons/pptx.png'
import txtIcon from '@/assets/file-icons/txt.png'
import videoIcon from '@/assets/file-icons/video.png'
import wavIcon from '@/assets/file-icons/wav.png'
import xlsxIcon from '@/assets/file-icons/xlsx.png'

const EXT_ICONS: Record<string, string> = {
  // 文档
  pdf: pdfIcon,
  doc: docxIcon, docx: docxIcon, dot: docxIcon, dotx: docxIcon, rtf: docxIcon,
  xls: xlsxIcon, xlsx: xlsxIcon, xlsm: xlsxIcon,
  csv: csvIcon,
  ppt: pptxIcon, pptx: pptxIcon, pot: pptxIcon, potx: pptxIcon,
  md: mdIcon, markdown: mdIcon,
  txt: txtIcon, log: txtIcon, text: txtIcon,
  // 代码 / 结构化
  json: jsonIcon,
  html: codeIcon, htm: codeIcon, xml: codeIcon, yml: codeIcon, yaml: codeIcon,
  js: codeIcon, ts: codeIcon, css: codeIcon, py: codeIcon, sql: codeIcon, sh: codeIcon,
  // 图片
  png: pngIcon, gif: pngIcon, webp: pngIcon, bmp: pngIcon, svg: pngIcon, tiff: pngIcon,
  jpg: jpgIcon, jpeg: jpgIcon,
  // 音频
  mp3: audioIcon, wav: wavIcon, m4a: audioIcon, flac: audioIcon, aac: audioIcon, ogg: audioIcon,
  // 视频
  mp4: videoIcon, mov: videoIcon, avi: videoIcon, mkv: videoIcon, webm: videoIcon, wmv: videoIcon,
}

export function fileIcon(name: string): string {
  const ext = name.includes('.') ? name.split('.').pop()!.toLowerCase() : ''
  return EXT_ICONS[ext] || txtIcon
}
