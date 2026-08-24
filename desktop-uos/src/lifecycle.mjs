export function configureUosRuntime(app) {
  app.disableHardwareAcceleration();
}

export function startWhenReady({ app, initialize, onError }) {
  void app.whenReady().then(initialize).catch(onError);
}
