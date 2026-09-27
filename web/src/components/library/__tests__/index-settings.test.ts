import { describe, it, expect } from 'vitest'
import { defaultSettings, settingsFromConfig, settingsToConfig, librarySettingsFromConfig } from '../index-settings'
import type { DocumentIndexConfig, ProcessingConfig } from '@/api/document-library'

const libProcessing = (over: Partial<ProcessingConfig> = {}): ProcessingConfig => ({
  chunk_method: 'naive', layout_recognize: 'DeepDOC', chunk_token_num: 512, delimiter: '\n。！？；',
  embedding_model: '', overlap: 25, replace_whitespace: false, remove_urls_emails: false,
  enable_children: false, children_delimiter: '\n', auto_keywords: 0, auto_questions: 0, ...over,
})

describe('settingsFromConfig', () => {
  it('未配置文档（config={}）从库级 naive 推导为 custom', () => {
    // 后端对未配置文档返回空对象 {}（truthy），不能误判为已配置导致 strategy=undefined
    const s = settingsFromConfig({} as DocumentIndexConfig, libProcessing())
    expect(s.strategy).toBe('custom')
    expect(s.method).toBe('naive')
  })

  it('未配置文档从库级 auto 推导为 auto', () => {
    const s = settingsFromConfig({} as DocumentIndexConfig, libProcessing({chunk_method: 'auto'}))
    expect(s.strategy).toBe('auto')
  })

  it('未配置文档从库级父子配置推导为 parent_child', () => {
    const s = settingsFromConfig({} as DocumentIndexConfig, libProcessing({enable_children: true}))
    expect(s.strategy).toBe('parent_child')
  })

  it('null config 同样走推导分支', () => {
    expect(settingsFromConfig(null, libProcessing()).strategy).toBe('custom')
    expect(settingsFromConfig(undefined, libProcessing({chunk_method: 'auto'})).strategy).toBe('auto')
  })

  it('已配置文档以保存的 strategy 为准', () => {
    const cfg: DocumentIndexConfig = {processing: libProcessing({chunk_method: 'auto'}), strategy: 'auto', enhancements: {}, type_rules: {}}
    const s = settingsFromConfig(cfg, libProcessing())
    expect(s.strategy).toBe('auto')
    expect(s.method).toBe('naive')  // auto 不是分段方式，表单回退 naive
  })

  it('未配置文档从库级全量配置取 processing 推导（不继承库级 by_file_type）', () => {
    const libCfg: DocumentIndexConfig = {processing: libProcessing({chunk_method: 'naive'}), strategy: 'by_file_type', enhancements: {}, type_rules: {}}
    const s = settingsFromConfig({} as DocumentIndexConfig, libCfg)
    expect(s.method).toBe('naive')
    expect(s.strategy).toBe('custom')  // 文档视图按 processing 推导，scope=document 不展示按文件类型
  })
})

describe('librarySettingsFromConfig 库级回填', () => {
  it('全量配置完整还原策略视图（按文件类型 + 规则 + 增强）', () => {
    const cfg: DocumentIndexConfig = {
      processing: libProcessing({chunk_method: 'paper', chunk_token_num: 300, overlap: 40}),
      strategy: 'by_file_type',
      enhancements: {include_filename: false},
      type_rules: {pdf: {strategy: 'custom', method: 'paper', chunk_token_num: 300, delimiter: '。', children_delimiter: '\n'}},
    }
    const s = librarySettingsFromConfig(cfg)
    expect(s.strategy).toBe('by_file_type')
    expect(s.method).toBe('paper')
    expect(s.chunk_token_num).toBe(300)
    expect(s.overlap).toBe(40)
    expect(s.type_rules.pdf.method).toBe('paper')
    expect(s.enhancements.include_filename).toBe(false)
    expect(s.enhancements.auto_summary).toBe(true)  // 未配置项回落默认
  })

  it('旧库缺省 strategy 时按 processing 推导，空配置返回默认自动', () => {
    const legacy = {processing: libProcessing(), enhancements: {}, type_rules: {}} as DocumentIndexConfig
    expect(librarySettingsFromConfig(legacy).strategy).toBe('custom')
    const pc = {processing: libProcessing({enable_children: true}), enhancements: {}, type_rules: {}} as DocumentIndexConfig
    expect(librarySettingsFromConfig(pc).strategy).toBe('parent_child')
    expect(librarySettingsFromConfig(null).strategy).toBe('auto')
  })
})

describe('settingsToConfig 策略隔离', () => {
  it('auto：chunk_method=auto 且关闭父子', () => {
    const cfg = settingsToConfig({...defaultSettings(), strategy: 'auto'})
    expect(cfg.processing.chunk_method).toBe('auto')
    expect(cfg.processing.enable_children).toBe(false)
    expect(cfg.strategy).toBe('auto')
  })

  it('custom：用户参数真实透传', () => {
    const cfg = settingsToConfig({...defaultSettings(), strategy: 'custom', method: 'manual',
      chunk_token_num: 300, delimiter: '。', overlap: 40,
      preprocess: {replace_whitespace: true, remove_urls_emails: true}})
    expect(cfg.processing.chunk_method).toBe('manual')
    expect(cfg.processing.chunk_token_num).toBe(300)
    expect(cfg.processing.delimiter).toBe('。')
    expect(cfg.processing.overlap).toBe(40)
    expect(cfg.processing.replace_whitespace).toBe(true)
    expect(cfg.processing.remove_urls_emails).toBe(true)
    expect(cfg.processing.enable_children).toBe(false)
  })

  it('parent_child：naive + enable_children + 子分段标识符', () => {
    const cfg = settingsToConfig({...defaultSettings(), strategy: 'parent_child', children_delimiter: '\n\n'})
    expect(cfg.processing.chunk_method).toBe('naive')
    expect(cfg.processing.enable_children).toBe(true)
    expect(cfg.processing.children_delimiter).toBe('\n\n')
  })

  it('策略切换无残留：父子 → 自动后 enable_children 归零', () => {
    const pc = settingsToConfig({...defaultSettings(), strategy: 'parent_child'})
    const reopened = settingsFromConfig(pc, null)
    const auto = settingsToConfig({...reopened, strategy: 'auto'})
    expect(auto.processing.enable_children).toBe(false)
    expect(auto.processing.chunk_method).toBe('auto')
  })

  it('策略切换无残留：自动 → 自定义后 chunk_method 回落所选方式', () => {
    const auto = settingsToConfig({...defaultSettings(), strategy: 'auto'})
    const reopened = settingsFromConfig(auto, null)
    const custom = settingsToConfig({...reopened, strategy: 'custom', method: 'paper'})
    expect(custom.processing.chunk_method).toBe('paper')
    expect(custom.processing.enable_children).toBe(false)
  })
})
