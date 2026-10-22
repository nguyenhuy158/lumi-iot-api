Java.perform(function () {
  const Cipher = Java.use('javax.crypto.Cipher');
  const String = Java.use('java.lang.String');
  let captured = false;
  for (const method of Cipher.doFinal.overloads) {
    if (method.returnType.className !== '[B') continue;
    method.implementation = function () {
      const result = method.apply(this, arguments);
      if (!captured && result !== null && result.length > 500 && result.length < 10000) {
        const value = String.$new(result, 'UTF-8').toString();
        if (value.includes('-----BEGIN RSA PRIVATE KEY-----') || value.includes('-----BEGIN PRIVATE KEY-----')) {
          captured = true;
          console.log('LUMI_PRIVATE_KEY_JSON=' + JSON.stringify(value));
        }
      }
      return result;
    };
  }
  console.log('Signing-key observation ready');
});
