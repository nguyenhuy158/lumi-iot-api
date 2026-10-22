const certificate = {"offset": 7297476, "expected": "ff4301d1fe5f02a9ed690c942d6b0c94", "hex": "308203af308202970214181e853aea2e30fe353eb56de45cc5e9ee0aaff5300d06092a864886f70d01010b0500308192310b300906035504061302564e310f300d06035504080c064861204e6f69310f300d06035504070c064861204e6f693111300f060355040a0c084c756d69204a534331133011060355040b0c0a53657276657220522644311b301906035504030c126c756d692d7468696e672e6c756d692e766e311c301a06092a864886f70d010901160d6461696e64406c756d692e766e3020170d3234303530333032313532355a180f33303233303930343032313532355a308192310b300906035504061302564e310f300d06035504080c064861204e6f69310f300d06035504070c064861204e6f693111300f060355040a0c084c756d69204a534331133011060355040b0c0a53657276657220522644311b301906035504030c126c756d692d7468696e672e6c756d692e766e311c301a06092a864886f70d010901160d6461696e64406c756d692e766e30820122300d06092a864886f70d01010105000382010f003082010a0282010100b68e69b15ee49ec24079cac991dd72603680c87f8d78248ec7c09e84ace0375e576a2e91595a8d184a50240ac90382f288f2c8c8fd6ebead4c4276c8f92b55b3c1a15ba479fbe4dc5e4653bd989a7b495734b17ac1e954ff39bdb93ed242fe7f5cc22b8177e5d9c83f69895ada696ad49b1676b5e678abd7f7f3ef189b1b75e0594dfb66fcfdc2ffa79a462caaeb5205506344a44702bb2a52d295d2dfdea1d68ad7eba8691a8e99950f2ee58cf0766065a51b4294759c711e811d338c65e822a95a4e379ed58354e05dffa8583113205dd136d1295ada56049c71b4af3dfdf5439aa921997a4d6a785d50d0efa1b8d116dfd59aba3d26a5e9cf67e5ae4fdcb50203010001300d06092a864886f70d01010b050003820101004f888af62cf64d942ba663f52bd52bf974abda064561f7781185c48710d84f7309a52cf77a68845d3991b16d4d83133188b8a061821114dfb7b89cf2e43f160ec1a5c6baebc1d564de5c39c3969bb5b7845c91dfde899b057f6cac59bc91ce80f24857c9b2628fbd915ae2f8114a5dba0cba8d2fa7bb14bfb4a7979058860f37488bfedf0783b20520b3ae86663f53593d50942826c8b6a6b38e3d8c8bf5fbfad99fba9e86e771786a4dac75b757816b5cd0dc067caaa5b859f7d7f56ac3464858d2aef966a88e38bd6b6b27deb2a610685f93125183a08a1fce0fe3cc462a5107420e5289c51d7f55f720d50811d99df36ca8bc65d2f89b7485149779b45e1b", "sha256": "05f35efad0e3c0820f369fff3c10d37b83a19466a2665b98ff7dc08154395bfc"};
const verified = {"sha256": "ff60911a10d881d7211bb7e6135159c6ddc90d18e6c177eea60bb3fe797b2888", "symbols": {"SSL_read": 7436008, "SSL_write": 7438064}, "expected": {"SSL_read": "ff0306d1fd7b12a9fc6f13a9fa6714a9", "SSL_write": "ff0301d1fe5f01a943e10b94f40300aa"}};
let hooked = false;
function install(m) {
  if (hooked || m.name !== 'libflutter.so') return;
  hooked = true;
for (const name of ['SSL_write', 'SSL_read']) {
  const address = m.base.add(verified.symbols[name]);
  const actual = Array.from(new Uint8Array(address.readByteArray(16))).map(v => v.toString(16).padStart(2, '0')).join('');
  if (actual !== verified.expected[name]) throw new Error('Instruction mismatch: ' + name);
  Interceptor.attach(address, {
    onEnter(args) {
      this.ssl = args[0].toString();
      this.buffer = args[1];
      this.length = args[2].toInt32();
      if (name === 'SSL_write' && this.length > 0 && this.length <= 1048576) {
        send({kind: 'tls_plaintext', direction: 'out', ssl: this.ssl, length: this.length, time: Date.now()}, this.buffer.readByteArray(this.length));
      }
    },
    onLeave(retval) {
      const length = retval.toInt32();
      if (name === 'SSL_read' && length > 0 && length <= this.length && length <= 1048576) {
        send({kind: 'tls_plaintext', direction: 'in', ssl: this.ssl, length, time: Date.now()}, this.buffer.readByteArray(length));
      }
    }
  });
}
const certAddress = m.base.add(certificate.offset);
const certInstructions = Array.from(new Uint8Array(certAddress.readByteArray(16))).map(v => v.toString(16).padStart(2, '0')).join('');
if (certInstructions !== certificate.expected) throw new Error('Certificate hook instruction mismatch');
Interceptor.attach(certAddress, {
  onEnter(args) {
    const length = args[1].toInt32();
    if (length !== certificate.hex.length / 2) return;
    const value = Array.from(new Uint8Array(args[0].readByteArray(length))).map(v => v.toString(16).padStart(2, '0')).join('');
    if (value === certificate.hex) send({kind: 'certificate_seen', sha256: certificate.sha256, time: Date.now()});
  }
});
send({kind: 'ready', pid: Process.id, functions: ['SSL_write', 'SSL_read']});
}
Process.attachModuleObserver({ onAdded: install });
