import { t } from './i18n/index.ts'

export type Recipe = {
  id: string
  title: string
  hint: string
}

export const RECIPES: Recipe[] = [
  {
    id: 'guvenlik',
    title: t('work.recipe.guvenlik.title'),
    hint: t('work.recipe.guvenlik.hint'),
  },
  {
    id: 'hata',
    title: t('work.recipe.hata.title'),
    hint: t('work.recipe.hata.hint'),
  },
  {
    id: 'mimari',
    title: t('work.recipe.mimari.title'),
    hint: t('work.recipe.mimari.hint'),
  },
  {
    id: 'arayuz',
    title: t('work.recipe.arayuz.title'),
    hint: t('work.recipe.arayuz.hint'),
  },
]
