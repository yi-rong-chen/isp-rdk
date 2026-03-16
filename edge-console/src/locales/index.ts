import i18n from 'i18next';
import { initReactI18next } from 'react-i18next';

import zhCN from './zh-CN';
import enUS from './en-US';

// 配置 i18next
i18n
  .use(initReactI18next)
  .init({
    resources: {
      'zh-CN': {
        translation: zhCN,
      },
      'en-US': {
        translation: enUS,
      },
    },
    lng: 'zh-CN', // 默认语言
    fallbackLng: 'zh-CN',
    interpolation: {
      escapeValue: false,
    },
    react: {
      useSuspense: false,
    },
  });

export default i18n; 