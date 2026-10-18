import { BaseApp } from '@zeppos/zml/base-app'

App(
  BaseApp({
    globalData: {},
    onCreate() {
      console.log('convo test app: create')
    },
    onDestroy() {
      console.log('convo test app: destroy')
    },
  }),
)
