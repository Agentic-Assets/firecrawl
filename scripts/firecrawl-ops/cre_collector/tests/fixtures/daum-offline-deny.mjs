
import http from 'node:http';
import https from 'node:https';
import net from 'node:net';
import tls from 'node:tls';
import {syncBuiltinESMExports} from 'node:module';
export const denied={fetch:0,native:0,nativeKinds:[]};
globalThis.__DAUM_OFFLINE_DENIED=true;
globalThis.fetch=async()=>{denied.fetch++;throw new Error('OFFLINE_FETCH_DENIED');};
const block=kind=>()=>{denied.native++;denied.nativeKinds.push(kind);throw new Error('OFFLINE_NATIVE_NETWORK_DENIED');};
http.request=block('http.request');http.get=block('http.get');
https.request=block('https.request');https.get=block('https.get');
net.Socket.prototype.connect=block('Socket.connect');
net.connect=block('net.connect');net.createConnection=block('net.createConnection');tls.connect=block('tls.connect');
syncBuiltinESMExports();
