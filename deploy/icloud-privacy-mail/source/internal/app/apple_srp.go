package app

import (
	"crypto/pbkdf2"
	"crypto/rand"
	"crypto/sha256"
	"encoding/hex"
	"fmt"
	"hash"
	"math/big"
)

var (
	srpN = mustParseBigHex(
		"AC6BDB41324A9A9BF166DE5E1389582FAF72B6651987EE07FC3192943DB56050" +
			"A37329CBB4A099ED8193E0757767A13DD52312AB4B03310DCD7F48A9DA04FD50" +
			"E8083969EDB767B0CF6095179A163AB3661A05FBD5FAAAE82918A9962F0B93B8" +
			"55F97993EC975EEAA80D740ADBF4FF747359D041D5C33EA71D281E446B14773B" +
			"CA97B43A23FB801676BD207A436C6481F1D2B9078717461A5B9D32E688F87748" +
			"544523B524B0D57D5EA77A2775D2ECFA032CFBDBF52FB3786160279004E57AE" +
			"6AF874E7303CE53299CCC041C7BC308D82A5698F3A8D0C38271AE35F8E9DBFB" +
			"B694B5C803D89F7AE435DE236D525F54759B65E372FCD68EF20FA7111F9E4AFF73")
	srpG         = big.NewInt(2)
	srpNLenBytes = 256
)

type srpClient struct {
	a  *big.Int
	A  *big.Int
	k  *big.Int
	M1 []byte
	M2 []byte
}

func mustParseBigHex(s string) *big.Int {
	b, ok := new(big.Int).SetString(s, 16)
	if !ok {
		panic("bad SRP N constant")
	}
	return b
}

func newSRPClient() (*srpClient, error) {
	secret := make([]byte, 32)
	if _, err := rand.Read(secret); err != nil {
		return nil, fmt.Errorf("srp random: %w", err)
	}
	a := new(big.Int).SetBytes(secret)
	return &srpClient{
		a: a,
		A: new(big.Int).Exp(srpG, a, srpN),
		k: srpMultiplier(),
	}, nil
}

func (c *srpClient) ABytes() []byte {
	return srpPad(c.A)
}

func (c *srpClient) processChallenge(username, derivedKey, salt, serverB []byte) error {
	B := new(big.Int).SetBytes(serverB)
	if B.Sign() <= 0 || B.Cmp(srpN) >= 0 {
		return fmt.Errorf("srp invalid server B")
	}
	x := srpX(salt, derivedKey)
	u := srpU(c.A, B)
	if u.Sign() == 0 {
		return fmt.Errorf("srp invalid u")
	}
	S := srpS(c.k, x, c.a, B, u)
	K := srpHash(S)
	aBytes := srpPad(c.A)
	bBytes := srpPad(B)
	c.M1 = srpM1(username, salt, aBytes, bBytes, K)
	c.M2 = srpM2(aBytes, c.M1, K)
	return nil
}

func deriveAppleSRPPassword(password string, salt []byte, iterations int, protocol string) ([]byte, error) {
	passHash := sha256.Sum256([]byte(password))
	var input string
	switch protocol {
	case "s2k":
		input = string(passHash[:])
	case "s2k_fo":
		input = hex.EncodeToString(passHash[:])
	default:
		return nil, fmt.Errorf("unsupported SRP protocol %q", protocol)
	}
	return pbkdf2.Key(sha256.New, input, salt, iterations, 32)
}

func srpPad(n *big.Int) []byte {
	b := n.Bytes()
	if len(b) >= srpNLenBytes {
		return b
	}
	out := make([]byte, srpNLenBytes)
	copy(out[srpNLenBytes-len(b):], b)
	return out
}

func srpHash(data []byte) []byte {
	h := sha256.New()
	h.Write(data)
	return h.Sum(nil)
}

func srpHashToInt(h hash.Hash) *big.Int {
	return new(big.Int).SetBytes(h.Sum(nil))
}

func srpMultiplier() *big.Int {
	h := sha256.New()
	nBytes := srpN.Bytes()
	gBytes := srpG.Bytes()
	for len(gBytes) < len(nBytes) {
		gBytes = append([]byte{0}, gBytes...)
	}
	h.Write(nBytes)
	h.Write(gBytes)
	return srpHashToInt(h)
}

func srpX(salt, derivedKey []byte) *big.Int {
	h := sha256.New()
	h.Write([]byte(":"))
	h.Write(derivedKey)
	inner := h.Sum(nil)

	h2 := sha256.New()
	h2.Write(salt)
	h2.Write(inner)
	return new(big.Int).SetBytes(h2.Sum(nil))
}

func srpU(A, B *big.Int) *big.Int {
	h := sha256.New()
	h.Write(srpPad(A))
	h.Write(srpPad(B))
	return srpHashToInt(h)
}

func srpS(k, x, a, B, u *big.Int) []byte {
	gx := new(big.Int).Exp(srpG, x, srpN)
	kgx := new(big.Int).Mul(k, gx)
	diff := new(big.Int).Sub(B, kgx)
	ux := new(big.Int).Mul(u, x)
	exp := new(big.Int).Add(a, ux)
	S := new(big.Int).Exp(diff, exp, srpN)
	S.Mod(S, srpN)
	return srpPad(S)
}

func srpM1(username, salt, A, B, K []byte) []byte {
	hg := srpHash(srpPad(srpG))
	hn := srpHash(srpN.Bytes())
	hxor := make([]byte, len(hg))
	for i := range hg {
		hxor[i] = hg[i] ^ hn[i]
	}
	h := sha256.New()
	h.Write(hxor)
	h.Write(srpHash(username))
	h.Write(salt)
	h.Write(A)
	h.Write(B)
	h.Write(K)
	return h.Sum(nil)
}

func srpM2(A, M1, K []byte) []byte {
	h := sha256.New()
	h.Write(A)
	h.Write(M1)
	h.Write(K)
	return h.Sum(nil)
}
